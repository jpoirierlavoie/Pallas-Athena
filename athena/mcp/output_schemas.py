"""Declared ``outputSchema`` for every MCP tool (wired into tools/list).

These schemas are a CONTRACT, not documentation: per the MCP spec
(2025-06-18), a tool that declares an ``outputSchema`` MUST return
``structuredContent`` conforming to it. Two consequences drive the style:

* **Never ``additionalProperties: false``.** Correct on inputs (a security
  control), poison on outputs: adding one field to a payload would make
  every strict client reject an otherwise valid response. Schemas here
  constrain what exists; they never forbid growth.
* **``required`` lists only always-present keys.** Conditionally emitted
  keys (``list_documents.folder_path``, only when ``folder_id`` was given)
  are typed but not required. ``tests/test_mcp_output_schemas.py`` runs
  every REAL handler over fixtures covering each ``anyOf`` branch and
  validates the actual payload against these schemas — a declared contract
  the handlers violate fails the deploy gate, not the client.

Multi-shape payloads (found/not-found, global/dossier) use ``anyOf`` with
an ``enum`` discriminator on the branch key, so a wrong-shape payload can
never satisfy the other branch by accident.

Conventions (§10.1): money is ``<field>_cents`` (int) + ``<field>_display``
(fr-CA string); date-only values are ``YYYY-MM-DD`` strings; true
timestamps are ISO-8601 America/Montreal strings. Nullable fields use JSON
Schema union types (``["string", "null"]``).

Pure data — imports nothing from the package, so ``mcp/tools.py`` can
import it with no cycle.
"""

from typing import Any, Optional

# ── Fragment helpers ────────────────────────────────────────────────────
# Fresh dicts everywhere (no shared mutable fragments): a schema is data
# that ends up serialized into tools/list, and a shared reference edited
# "just for one tool" would silently edit them all.


def _obj(
    properties: dict[str, Any],
    required: Optional[list[str]] = None,
    description: str = "",
    optional: tuple[str, ...] = (),
) -> dict:
    """An object schema. ``required=None`` requires EVERY listed key —
    the common case, since handlers build their dicts unconditionally —
    except those named in *optional*, the explicit carve-out for keys added
    to an existing contract (see :data:`_PROVENANCE_KEYS`)."""
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    keys = (
        [k for k in properties if k not in optional]
        if required is None else required
    )
    if keys:
        schema["required"] = keys
    if description:
        schema["description"] = description
    return schema


def _arr(items: dict, description: str = "") -> dict:
    schema: dict[str, Any] = {"type": "array", "items": items}
    if description:
        schema["description"] = description
    return schema


def _str(description: str = "") -> dict:
    return {"type": "string", "description": description} if description else {
        "type": "string"
    }


def _nstr(description: str = "") -> dict:
    schema: dict[str, Any] = {"type": ["string", "null"]}
    if description:
        schema["description"] = description
    return schema


def _int(description: str = "") -> dict:
    return {"type": "integer", "description": description} if description else {
        "type": "integer"
    }


def _nint(description: str = "") -> dict:
    schema: dict[str, Any] = {"type": ["integer", "null"]}
    if description:
        schema["description"] = description
    return schema


def _num(description: str = "") -> dict:
    return {"type": "number", "description": description} if description else {
        "type": "number"
    }


def _bool(description: str = "") -> dict:
    return {"type": "boolean", "description": description} if description else {
        "type": "boolean"
    }


def _money(key: str, description: str = "") -> dict[str, Any]:
    """The §10.1 money pair, to splat into a properties dict.

    The optional description lands on the ``_cents`` field — the figure a
    reader computes with. Use it whenever the amount means something less
    obvious than its name suggests (a frozen figure, a derived one).
    """
    cents: dict[str, Any] = {"type": "integer"}
    if description:
        cents["description"] = description
    return {
        f"{key}_cents": cents,
        f"{key}_display": {"type": "string"},
    }


def _list_envelope(item_schema: dict, extra: Optional[dict] = None,
                   extra_required: Optional[list[str]] = None) -> dict:
    """The shared ``{items, count, truncated}`` list payload."""
    properties: dict[str, Any] = {
        "items": _arr(item_schema),
        "count": _int("Number of items returned (post-truncation)."),
        "truncated": _bool("true when more matches exist than were returned."),
    }
    if extra:
        properties.update(extra)
    required = ["items", "count", "truncated"] + (extra_required or [])
    return {"type": "object", "properties": properties, "required": required}


def _next_offset() -> dict[str, Any]:
    """G07 paging key of the materialized list tools — present ONLY when a
    next page exists (typed, never required)."""
    return {"next_offset": _int(
        "Pass as `offset` to fetch the next page. Absent on the last page."
    )}


def _next_cursor(note: str = "") -> dict[str, Any]:
    """Keyset paging key — ALWAYS present, null on the last page.

    Deliberately unlike ``_next_offset`` (typed but optional): an absent key
    is one a client forgets to test for, and « no more pages » must be an
    explicit null rather than an omission. Callers pass it through
    ``extra_required`` so the contract enforces the presence.
    """
    return {"next_cursor": _nstr(
        "Pass as `cursor` for the next page; null when this is the last "
        "page." + (" " + note if note else "")
    )}


def _found_or_not(found_schema: dict, notfound_props: dict[str, Any]) -> dict:
    """anyOf(found=true shape, found=false shape), enum-discriminated.

    The root carries ``type: "object"`` BESIDE the anyOf: the MCP wire
    schema for ``Tool.outputSchema`` requires a top-level ``type`` with
    const ``object`` (the official SDK zod-parses the whole ListToolsResult,
    so ONE bare-anyOf descriptor would fail EVERY tool at once). Draft
    2020-12 applies type and anyOf conjunctively and every branch is itself
    an object, so payload acceptance is unchanged.
    """
    notfound = _obj(
        {"found": _found(False), **notfound_props},
        description="The requested record does not exist — absence is data, "
        "never an all-zero fabrication.",
    )
    return {"type": "object", "anyOf": [found_schema, notfound]}


def _found(value: bool) -> dict[str, Any]:
    """The anyOf discriminator, as a FRESH dict per usage (module rule)."""
    return {"type": "boolean", "enum": [value]}

# A model-owned summary passed through verbatim. Typed loosely on purpose:
# constraining a shape this module does not build would make the schema a
# SECOND copy of the model's contract, drifting silently.
def _model_summary(description: str) -> dict:
    return {"type": "object", "description": description}


# ── Shared row schemas ──────────────────────────────────────────────────

def _dossier_entity() -> dict:
    """A dossier write's entity snapshot (``handlers._dossier_entity``)."""
    return _obj({
        "id": _str(),
        "dossier_id": _str("Same value as id — the audit log reads this."),
        "file_number": _str(),
        "label": _str("The dossier title."),
        "status": _str(),
        "legacy_ref": _str("'' when not imported."),
        **_written_etag(),
    }, optional=("etag",))


def _dossier_write_result(
    verb: str, extra: Optional[dict[str, Any]] = None,
    optional: tuple[str, ...] = (),
) -> dict:
    """The success payload of a dossier write — plus *extra* keys (lot 4b's
    status and link tools), *optional* naming those not always emitted.

    NO ctag_bumped / dav_synced: dossiers are not a DAV collection, and this
    module's rule is never to declare a sync key a write cannot honour.
    set_dossier_status reports what its status did to the phone in its own
    ``dav`` block instead.
    """
    return _obj({
        verb: {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « dossier »."),
        "entity": _dossier_entity(),
        "prescription_date": _nstr(
            "The « date pour agir » AFTER the model recomputed it — it is "
            "derived from droit_action_date + the confirmed delay, never "
            "imported verbatim. A warning says so when it fired."
        ),
        "prescription_status": _str(
            "courante | interrompue | echue | imprescriptible | a_verifier."
        ),
        "warnings": _arr(_str(
            "French. Names anything the models rewrote or discarded: a "
            "computed prescription date, a district cleared by the forum "
            "rules, a file number forced to « Préjudiciaire », a dossier "
            "created closed and therefore never advertised to DavX5."
        )),
        **_write_protocol_keys(),
        **(extra or {}),
    }, optional=optional)


def _party_link() -> dict:
    """``update_dossier_party``'s party block — update or remove."""
    return _obj({
        "partie_id": _str("The party's contact id."),
        "side": _str("clients | opposing_parties — the side it is (or was) on."),
        "name": _str("The party's name snapshot on the dossier."),
        "roles_before": _arr(_str(), "update: the roles before."),
        "roles_after": _arr(_str(), "update: the roles as stored now."),
        "avocat_id_before": _str("update: the lawyer's contact id before; ''."),
        "avocat_id_after": _str("update: the lawyer's contact id now; ''."),
        "avocat_name_after": _str("update: the lawyer's name snapshot now."),
        "was_first_client": _bool(
            "remove: it was the FIRST client — the default invoice "
            "recipient moves to the next one."),
        "journaled": _bool(
            "remove: the detach is in the deletion journal (list_deletions, "
            "dossier_party)."),
    }, required=["partie_id", "side", "name"])


def _party_refresh_row() -> dict:
    """One dossier of ``update_dossier_party``'s refresh_names."""
    return _obj({
        "dossier_id": _str(),
        "file_number": _str(),
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged", "refused"],
            "description": (
                "« unchanged » = every name was current, nothing written; "
                "« refused » = this dossier's save was refused (reason) — the "
                "others went on."),
        },
        "reason": _nstr("French, on « refused »; null otherwise."),
        "etag": _str("The dossier's etag as stored after this row."),
        "changes": _arr(_obj({
            "partie_id": _str(),
            "side": _str(),
            "field": {"type": "string", "enum": ["name", "avocat_name"]},
            "before": _str(),
            "after": _str(),
        })),
        "missing_partie_ids": _arr(_str(), (
            "Contacts cited that no longer exist: their stored name is "
            "kept.")),
        "prescription_date_moved": _bool(
            "The save re-derived a different « date pour agir »."),
    }, optional=("etag",))


def _partie_write_result(
    verb: str, extra: Optional[dict[str, Any]] = None,
) -> dict:
    """The success payload of a contact write — plus *extra* keys (lot 4b's
    representation and compliance tools).

    Carries ctag_bumped/dav_synced because parties ARE DAV-exposed (CardDAV,
    /dav/addressbook/) — unlike time entries and disbursements, whose result
    deliberately declares neither rather than fake a sync that does not
    exist. Both are ``false`` on a lot-4b no-op, with no warning: nothing
    was written, so nothing had to reach the phone.
    """
    return _obj({
        verb: {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « partie »."),
        "entity": _obj({
            "id": _str(),
            "dossier_id": _str("Always '' — a contact belongs to no dossier."),
            "label": _str("display_name: legal name for an organization."),
            "type": _str("individual | organization."),
            "contact_role": _str(),
            "legacy_ref": _str("'' when not imported."),
            **_written_etag(),
        }, optional=("etag",)),
        "ctag_bumped": _bool(
            "The addressbook CTag moved, so DavX5 will re-sync. false with a "
            "warning means the write landed but the sync was not triggered — "
            "do NOT retry, it would duplicate the contact; false with no "
            "such warning means nothing was written (a no-op)."
        ),
        "dav_synced": _bool(
            "The contact will reach the phone; false on a no-op."),
        "warnings": _arr(_str("French; empty when nothing is amiss.")),
        **_write_protocol_keys(),
        **(extra or {}),
    })


def _audit_block() -> dict[str, Any]:
    """Totals over one population (time entries, or disbursements) as the
    import audit reports it."""
    block: dict[str, Any] = {
        "count": _int(),
        "invoiced_count": _int(),
        "uninvoiced_count": _int(),
        "created_via_mcp_count": _int("How many this connector wrote."),
        "unphased_count": _int("Rows with no phase code — '' is a real state."),
    }
    block.update(_money("amount"))
    block.update(_money("uninvoiced_amount"))
    return _obj(block)


def _phase_pair() -> dict[str, Any]:
    """The Phase O classification carried by a time entry, expense or task.

    Code AND bare label. The connector writes this pair and, until August
    2026, no row read it back — a classification it could not verify. The
    empty string is a real value in the vocabulary (« non renseignée »), not
    a missing field, so every key is always present.
    """
    return {
        "phase": _str(
            "Litigation phase code, e.g. « CTS ». '' = non renseignée, which "
            "is a legitimate state: legacy rows were never back-filled."
        ),
        "sous_phase": _str("Sub-code, e.g. « CTS-02 ». '' = non renseignée."),
        "phase_label": _str(
            "French label of `phase`, e.g. « Contestation ». « Non "
            "renseignée » when the code is ''."
        ),
        "sous_phase_label": _str("French label of `sous_phase`."),
    }


# The concurrency token and the provenance stamps, added to existing
# contracts on 2026-09-25 (plan lot 0a). The handlers ALWAYS emit them, yet
# every schema declares them OPTIONAL — never required — for two reasons: an
# idempotency replay may hand back a write result stored before they existed
# (the cache lives 24 h), and a key an existing contract never promised must
# not become one a strict client can reject a response over.
# ``tests/test_mcp_output_schemas.py`` sweeps every schema for it.
_PROVENANCE_KEYS: tuple[str, ...] = (
    "etag", "created_via", "updated_via", "mcp_updated_at",
)

_VIA_VOCABULARY = "web | dav | mcp | cron | script"

# list_time_entries / list_expenses already REQUIRED created_via before the
# provenance keys existed (it was « 'mcp' or '' »): it stays required there,
# only the three new keys are carved out.
_BILLING_ROW_OPTIONAL: tuple[str, ...] = tuple(
    k for k in _PROVENANCE_KEYS if k != "created_via"
)


def _provenance() -> dict[str, Any]:
    """The four keys of :data:`_PROVENANCE_KEYS`, typed and described.

    No date in these texts, on purpose: the boundary between an unrecorded
    and a recorded path is the day provenance was DEPLOYED, which no commit
    can know — a date written here would be false for every record written
    between it and the deploy. « Not recorded » is true whatever the day."""
    return {
        "etag": _str(
            "Concurrency token of the stored record, regenerated by every "
            "write. '' on a legacy document that never carried one."),
        "created_via": _str(
            f"Path that created the record: {_VIA_VOCABULARY} (web = the "
            "application, dav = the phone through DavX5, mcp = this "
            "connector, cron = a scheduled or queued task, script = a "
            "maintenance script). '' = not recorded: the record predates "
            "provenance."),
        "updated_via": _str(
            f"Path of the LAST write, same vocabulary ({_VIA_VOCABULARY}). "
            "'' = not recorded: no write since provenance began."),
        "mcp_updated_at": _nstr(
            "ISO-8601 Montréal: the last write made through this connector. "
            "Sticky — a later application or phone write never clears it. "
            "null = no connector write recorded since provenance began."),
    }


def _written_etag() -> dict[str, Any]:
    """``etag`` on the entity of an EDITABLE record's write result.

    Declared OPTIONAL wherever it is spread (``optional=("etag",)``): the
    handlers always emit it since 2026-09-25, but an idempotency replay may
    return a result stored before then (the cache lives 24 h), and a key an
    existing contract never promised must not become one a strict client
    rejects a response over."""
    return {
        "etag": _str(
            "The record's etag AS STORED after this write — pass it as "
            "`expected_etag` to the next edit instead of re-reading. On an "
            "« unchanged » outcome it is the stored etag, untouched."),
    }


_STEP_OVERDUE_STAMP = "« en_retard »"


def _step_provenance() -> dict[str, Any]:
    """The four keys on a PROTOCOL STEP row — the generic texts, but for
    the etag: steps are stamped since lot 1a, and every write to one moves
    its etag EXCEPT the deadline-derived « en_retard » stamp a protocol page
    view applies (``check_overdue_steps``) — a mere page view must not
    invalidate the etag a caller holds. That exception is the only thing a
    step row says differently, and ``tests/test_mcp_output_schemas.py`` pins
    it to step rows alone."""
    return {
        **_provenance(),
        "etag": _str(
            "Concurrency token of the stored step, regenerated by every "
            "write to it — except the deadline-derived "
            f"{_STEP_OVERDUE_STAMP} stamp a protocol page view applies, "
            "which never touches it. '' on a step written before steps "
            "carried one."),
    }


def _billing_created_via() -> dict:
    """created_via on a time-entry / expense row — a contract that predates
    provenance, so its old values keep their old meaning."""
    return _str(
        f"Path that recorded the entry: {_VIA_VOCABULARY}. « mcp » still "
        "means this connector recorded it — it has stamped its own entries "
        "since July 2026. '' = recorded by another path (the application or "
        "a script) before provenance began; an entry recorded since then "
        "reads its path (« web » for the application), never ''."
    )


def _stamps() -> dict[str, Any]:
    """The stamps every row carries: created_at/updated_at (PA-G05) — true
    instants (iso_mtl), nullable on pre-Rule-7 legacy docs; updated_at is
    NOISY: DAV round-trips, protocol-step syncs and bulk folder moves all
    re-stamp it without visible content changing — plus the four
    :data:`_PROVENANCE_KEYS`. An ``_obj`` that spreads this passes
    ``optional=_PROVENANCE_KEYS``."""
    return {
        "created_at": _nstr("ISO-8601 Montréal."),
        "updated_at": _nstr(
            "ISO-8601 Montréal. Noisy: phone syncs and internal "
            "bookkeeping re-stamp it without visible changes."),
        **_provenance(),
    }


# Keys a list_hearings row carries ONLY in the bookings "pending" mode — the
# requester and the contact a confirmation would link. Typed, never
# required: the window and serie modes never emit them (the folder_path
# rule), and a CONFIRMED Bookings event's requester stays out of the
# ordinary agenda rows.
_PENDING_ROW_KEYS: tuple[str, ...] = (
    "confirmation", "client_nom", "client_email", "partie_suggeree_id",
    "partie_suggeree_nom",
)


def _hearing_row() -> dict:
    return _obj({
        "id": _str(),
        "title": _str(),
        "hearing_type": _str(),
        "forum": _str("« judiciaire » or « extrajudiciaire », derived from the type."),
        "start": _nstr("ISO-8601 Montréal for timed events; YYYY-MM-DD for all-day."),
        "end": _nstr(),
        "all_day": _bool(),
        "location": _str(),
        "modalite": _str("« présentiel », « visioconférence » or « téléphonique »."),
        "modalite_label": _str(),
        "conference_uri": _str("Video link (http/https); empty unless visioconférence."),
        "court": _str(),
        "judge": _str(),
        "status": _str("French vocabulary; annulée hearings are included."),
        "notes": _str(),
        "dossier_id": _str("Empty string for a « Général » (standalone) event."),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "reminder_minutes": _nint(
            "Reminder before the start, in minutes; null when none is "
            "stored in a usable form."),
        "serie_id": _str(
            "'' = a standalone event; otherwise its recurring chain — list "
            "it whole with list_hearings(serie_id)."),
        "source": _str(
            "'' for an ordinary event; « bookings » for a « Bookings with "
            "me » reservation — once confirmed, update_hearing changes only "
            "its dossier and its notes (its Outlook meeting, the client's, "
            "is changed in Outlook)."),
        "confirmation": _str(
            "Bookings \"pending\" mode only: « à_confirmer » (awaiting a "
            "decision) or « annulée_client » (the client cancelled — "
            "refuser removes it). Every other row is a confirmed event."),
        "client_nom": _str("Bookings \"pending\" mode only: the requester."),
        "client_email": _str(
            "Bookings \"pending\" mode only: the requester's email."),
        "partie_suggeree_id": _str(
            "Bookings \"pending\" mode only: the contact whose email is "
            "exactly the requester's — the one decide_rendez_vous links; '' "
            "when none."),
        "partie_suggeree_nom": _str(
            "Bookings \"pending\" mode only: that contact's name."),
        **_stamps(),
    }, optional=_PROVENANCE_KEYS + _PENDING_ROW_KEYS)


def _task_row(extra: Optional[dict[str, Any]] = None) -> dict:
    properties: dict[str, Any] = {
        "id": _str(),
        "title": _str(),
        "description": _str(),
        "priority": _str(),
        "status": _str(),
        "category": _str(),
        "due_date": _nstr("YYYY-MM-DD; null for an undated task."),
        "is_overdue": _bool(
            "Due strictly BEFORE today (Montréal calendar). A task due TODAY "
            "is not overdue, an undated one never is, and a terminée/annulée "
            "one never is whatever its due date says."
        ),
        "completed_date": _nstr(),
        "dossier_id": _nstr("null for a « Général » (standalone) task."),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "related_note_id": _nstr("Linked parent note (RFC 5545 RELATED-TO)."),
        **_phase_pair(),
        **_stamps(),
    }
    if extra:
        properties.update(extra)
    return _obj(properties, optional=_PROVENANCE_KEYS)


def _step_row(extra: Optional[dict[str, Any]] = None) -> dict:
    properties: dict[str, Any] = {
        "id": _str(),
        "order": _int(),
        "title": _str(),
        "description": _str(),
        "cpc_reference": _str("E.g. « art. 246 C.p.c. »."),
        "deadline_date": _nstr("YYYY-MM-DD."),
        "status": _str(
            "DERIVED from the deadline against today (Montréal) — this is the "
            "value that governs. à_venir | en_cours | en_retard | complété."
        ),
        "status_stored": _str(
            "The word stored on the document, for provenance only. It is "
            "written solely when the lawyer opens the protocol page in the "
            "browser, and an « en_retard » there is never cleared — so it can "
            "lag reality indefinitely. Prefer `status`."
        ),
        "status_differs": _bool(
            "true when the stored word no longer matches the derived one — "
            "the document is stale, not the reading."
        ),
        "mandatory": _bool(),
        "deadline_locked": _bool(),
        "deadline_offset_days": _nint(
            "Days after the protocol's start date the template computes "
            "this deadline from (art. 83 C.p.c.): a step that has one moves "
            "with a new start date unless completed or a confirmed CS date. "
            "null on a custom step."),
        "date_confirmed": _bool(),
        "completed_date": _nstr(),
        "linked_task_id": _nstr(),
        "linked_hearing_id": _nstr(),
        "notes": _str(),
        "is_overdue": _bool(
            "Equivalent to `status == \"en_retard\"` — both come from one "
            "predicate, so they can never contradict each other."
        ),
        **_phase_pair(),
        **_stamps(),
        # After _stamps: the step etag's one exception (the overdue stamp).
        **_step_provenance(),
    }
    if extra:
        properties.update(extra)
    return _obj(properties, optional=_PROVENANCE_KEYS)


def _dossier_list_row() -> dict:
    return _obj({
        "id": _str(),
        "file_number": _str(),
        "title": _str(),
        "status": _str(),
        "domaine": _str("Taxonomy family code (e.g. REC); empty if unclassified."),
        "domaine_label": _str(),
        "role": _str(),
        "tribunal": _str(),
        "court_file_number": _str(),
        "opened_date": _nstr("YYYY-MM-DD."),
        "prescription_date": _nstr(
            "The RAW computed « date pour agir », YYYY-MM-DD — provenance, "
            "never recomputed after an interruption/suspension event."),
        "prescription_status": _str(
            "courante | interrompue | echue | imprescriptible | "
            "a_verifier. « interrompue » = DECLARED by the lawyer (a "
            "demande filed / prise d'action) — art. 2892 signification is "
            "not recorded, so treat it as declared, not verified. A past "
            "prescription_date with status interrompue is NOT a blown "
            "deadline."),
        "prescription_date_effective": _nstr(
            "YYYY-MM-DD — the date the delay actually runs to, after "
            "events; null when interrupted (art. 2896: until judgment) "
            "or not computable."),
        "clients": _arr(_str(), "Client NAMES (strings) in this summary row."),
        "opposing_parties": _arr(_str()),
        **_stamps(),
    }, optional=_PROVENANCE_KEYS)


def _invoice_row(extra: Optional[dict[str, Any]] = None,
                 optional: tuple[str, ...] = ()) -> dict:
    """SHARED by list_invoices, get_invoice and
    get_billing_snapshot.outstanding_invoices — one row shape, no drift.
    *optional* names extra keys that are typed but never required."""
    properties: dict[str, Any] = {
        "id": _str(),
        "invoice_number": _str(),
        "dossier_id": _str(),
        "dossier_file_number": _str("Snapshot taken at issuance, not the "
                                    "file's current number — an invoice must "
                                    "read as what was sent to the client."),
        "client_name": _str("Snapshot at issuance."),
        "date": _nstr("YYYY-MM-DD."),
        "due_date": _nstr("YYYY-MM-DD."),
        "status": _str("brouillon | envoyée | payée | en_retard | annulée."),
        "status_label": _str("French label of `status`."),
        "paid_date": _nstr("YYYY-MM-DD; null when no payment is recorded."),
        "payment_basis": {
            "type": "string",
            "enum": ["recorded", "none"],
            "description": (
                "\"recorded\" = an amount was posted in the accounting module; "
                "\"none\" = nothing recorded. With \"none\", a balance equal "
                "to the total means nothing has been RECORDED — NOT that "
                "nothing was paid. Status alone may still say « payée »."
            ),
        },
        **_money("total"),
        **_money("amount_due",
                 "The balance AT ISSUANCE (total − retainer applied). Frozen: "
                 "it is never updated and stays non-zero on a paid invoice. "
                 "Use `balance` for what is still owed."),
        **_money("amount_paid", "Recorded payment; 0 when none was entered."),
        **_money("balance", "amount_due − amount_paid — the live balance."),
    }
    if extra:
        properties.update(extra)
    return _obj(properties, optional=optional)


def _partie_ref() -> dict:
    # roles/avocat_* (July 2026) are typed but NOT required: read paths
    # normalize them in, but the contract only promises what every stored
    # generation of the document guarantees.
    return _obj(
        {
            "id": _str(),
            "name": _str(),
            "roles": _arr(_str(), "Litigation roles of THIS party (French "
                                  "vocabulary; may hold several, e.g. "
                                  "défendeur + demandeur reconventionnel)."),
            "avocat_id": _str("Contact id of this party's lawyer; empty "
                              "when none is recorded."),
            "avocat_name": _str("Snapshot of the lawyer's name."),
        },
        required=["id", "name"],
        description="Party snapshot as stored on the dossier.",
    )


def _address() -> dict:
    return _obj({
        "street": _str(),
        "unit": _str(),
        "city": _str(),
        "province": _str(),
        "postal_code": _str(),
        "country": _str(),
    })


def _written_note() -> dict:
    return _obj({
        "id": _str(),
        "dossier_id": _str("Empty string for a « Général » note."),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "title": _str(),
        "category": _str(),
        "content_length": _int("Stored length AFTER sanitization — compare "
                               "against what was sent to detect any loss."),
        "created_at": _nstr(),
        "updated_at": _nstr(),
        # The note AS WRITTEN: etag is the new one.
        **_provenance(),
    }, optional=_PROVENANCE_KEYS)


def _write_protocol_keys() -> dict[str, Any]:
    """idempotent_replay — emitted by EVERY write result (WP15).

    ``dry_run`` was emitted here too until 2026-08-27, when the preview
    was removed from the write protocol (see
    ``mcp/write_support.run_write``). Both the key and its entry in every
    ``required`` list went with it: an output schema is a MUST-conform
    contract, so leaving a required key the handlers no longer emit would
    have made a strict client reject every write response.
    """
    return {
        "idempotent_replay": _bool(
            "true = this call replayed a previous write's stored result "
            "(same idempotency_key) — nothing was written twice."),
    }


def _written_entity(
    extra: dict[str, Any], optional: tuple[str, ...] = ()
) -> dict:
    """The WP16 creators' entity snapshot — common core + per-tool keys.
    *optional* names keys added to an existing contract (``etag``)."""
    props: dict[str, Any] = {
        "id": _str("The stored id."),
        "dossier_id": _str("Empty for a « Général » (standalone) entity."),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "label": _str("Title/description snapshot."),
        "date": _nstr("The operative date (due/start/entry); YYYY-MM-DD "
                      "or ISO-Montréal for a timed event."),
    }
    props.update(extra)
    return _obj(props, optional=optional)


def _dossier_etag() -> dict[str, Any]:
    """``dossier_etag`` — the dossier's etag AS STORED after a recorder
    wrote to it (review correction, lot 0a). Top-level, never inside the
    signification/event entity, which is an ARRAY ENTRY with no etag of
    its own. Optional for the same reason as :func:`_written_etag`."""
    return {
        "dossier_etag": _str(
            "The DOSSIER's etag as stored after this write — pass it as "
            "update_dossier's `expected_etag` instead of re-reading."),
    }


def _entity_write_result(
    entity_extra: dict[str, Any], *, dav: bool, verb: str = "created",
    extra: Optional[dict[str, Any]] = None, relocates: bool = False,
    optional: tuple[str, ...] = (), entity_optional: tuple[str, ...] = (),
) -> dict:
    """Result contract of a WP16 creator / WP17 recorder.

    DAV-exposed entities (task, hearing) carry ctag_bumped/dav_synced;
    time entries, expenses and dossier-array additions are not DAV-exposed
    and deliberately do NOT declare those keys — faking them would claim a
    sync that does not exist.

    *relocates* — for a tool whose write can MOVE the record to another
    dossier, and whose handler therefore passes ``previous_dossier_id`` to
    ``handlers._entity_write_result`` (lot 1b's movers: update_task,
    update_note, update_hearing). It adds
    ``previous_collection_cleared``, REQUIRED because such a handler emits
    it on every call. A tool that does not relocate never declares it —
    ``_obj`` requires every listed key, so declaring it on a tool that
    never emits it would ship a violated contract;
    ``tests/test_mcp_dav_resync.py`` derives the pairing from the
    handlers."""
    if relocates and not dav:
        raise ValueError("only a DAV-exposed entity has a collection to leave")
    props: dict[str, Any] = {
        verb: {"type": "boolean", "enum": [True]},
        "entity_type": _str(),
        "entity": _written_entity(entity_extra, entity_optional),
        "warnings": _arr(_str(), "French, human-readable; empty when clean."),
        **_write_protocol_keys(),
    }
    if dav:
        props["ctag_bumped"] = _bool(
            "Whether the DavX5 sync trigger fired. false = the write "
            "COMMITTED but the phone will only catch up on the next "
            "change; do not retry.")
        props["dav_synced"] = _bool(
            "ctag_bumped AND the collection is visible to DavX5 (a "
            "fermé/archivé dossier's is not).")
    if relocates:
        props["previous_collection_cleared"] = _bool(
            "false = the record MOVED to another dossier and the OLD "
            "dossier's DavX5 collection could not be told it left: a stale "
            "copy may stay on the phone there. true otherwise, including "
            "when it did not move. The write COMMITTED either way; do not "
            "retry.")
    if extra:
        props.update(extra)
    return _obj(props, optional=optional)


# The hearing entity keys lot 1b (L7) added — every write result of an
# event carries them. On create_hearing (a contract that predates them)
# they are declared optional, the _written_etag rule.
_HEARING_ENTITY_ADDED: tuple[str, ...] = (
    "end", "status", "modalite", "conference_uri", "reminder_minutes",
    "serie_id", "source",
)


def _hearing_entity_extra() -> dict[str, Any]:
    return {
        "end": _nstr("ISO-8601 Montréal for a timed event. All-day: the "
                     "stored end's YYYY-MM-DD — equal to `date` for a "
                     "one-day event; a LATER date is the exclusive end of a "
                     "multi-day one."),
        "status": _str("confirmée | à_confirmer | reportée | annulée | "
                       "terminée."),
        "modalite": _str("présentiel | visioconférence | téléphonique."),
        "conference_uri": _str("Video link (http/https); '' when none."),
        "reminder_minutes": _nint("Reminder before the start, in minutes."),
        "serie_id": _str("'' = standalone; else its recurring chain."),
        "source": _str("'' ordinary; « bookings » = a Bookings "
                       "reservation."),
    }


def _hearing_series_result() -> dict:
    """create_hearing_series' contract. The series bumps its DAV
    collection INSIDE its write batch (models.hearing
    .create_hearing_series), so ``ctag_bumped`` is true on every success —
    the handler never bumps a second time."""
    occurrence = _obj({
        "id": _str("The occurrence's stored id."),
        "start": _nstr("ISO-8601 Montréal (timed) or YYYY-MM-DD (all-day)."),
        "end": _nstr(),
        "etag": _str("Its etag AS STORED — update_hearing's "
                     "`expected_etag`."),
    }, optional=("etag",))
    return _obj({
        "created": {"type": "boolean", "enum": [True]},
        "entity_type": _str("« hearing_series »."),
        "entity": _written_entity({
            "hearing_type": _str(),
            "forum": _str("Derived from the type."),
            "all_day": _bool(),
            "status": _str(),
            "modalite": _str(),
        }),
        "serie_id": _str("The chain's id — the entity id; list it with "
                         "list_hearings(serie_id)."),
        "frequency": _str(),
        "rule_label": _str("French, e.g. « Chaque mois — 12 occurrences »."),
        "occurrences_count": _int(),
        "occurrences": _arr(occurrence, "Chronological, the first first."),
        "ctag_bumped": _bool(
            "true: the sync trigger rode inside the same atomic write."),
        "dav_synced": _bool(
            "ctag_bumped AND the collection is visible to DavX5 (a "
            "fermé/archivé dossier's is not)."),
        "warnings": _arr(_str(), "French, human-readable; empty when clean."),
        **_write_protocol_keys(),
    })


def _decide_rendez_vous_result() -> dict:
    """decide_rendez_vous' contract — the decision, and what reached the
    client. The two DAV keys keep their meaning: a confirmation puts the
    event in its collection (and bumps it); a refusal concerns no
    collection — a pending request was never on the phone — so both read
    false there, which is not a failure. The one exception: a request
    confirmed elsewhere while Outlook was being asked to cancel it — the
    refusal wins and the service tombstones the event out of its
    collection, and ctag_bumped says so."""
    return _obj({
        "decided": {"type": "boolean", "enum": [True]},
        "entity_type": _str("« hearing »."),
        "entity": _written_entity({
            "status": _str(),
            "all_day": _bool(),
            "confirmation": _str(
                "The decision gate AS STORED: '' = confirmed (now in the "
                "calendar), « refusée »; still « à_confirmer » when the "
                "local write failed after Outlook was cancelled (see "
                "local_written)."),
            **_written_etag(),
        }, optional=("etag",)),
        "action": _str("confirmer | refuser."),
        "changed": _bool(
            "false = that decision was already stored: nothing was "
            "written, nobody was contacted (a safe replay)."),
        "partie_liee": _bool("confirmer: a contact was linked."),
        "partie_id": _str("The linked contact's id; '' when none."),
        "local_written": _bool(
            "Whether Athéna recorded the decision. false with "
            "graph_cancelled true = the client WAS notified but the refusal "
            "could not be recorded — do NOT call again (see warnings)."),
        "graph_attempted": _bool("refuser: Outlook was asked to cancel."),
        "graph_cancelled": _bool(
            "refuser: Outlook accepted the cancellation."),
        "client_notified": _bool(
            "true = the cancellation went out, and Bookings sends it to "
            "the client."),
        "cancellation_message": _nstr(
            "The FIXED text sent with that cancellation; null when nothing "
            "was sent."),
        "ctag_bumped": _bool(
            "confirmer: whether the DavX5 sync trigger fired (false with a "
            "warning = the event is confirmed and will reach the phone at "
            "the next change; do not retry). refuser: false (a pending "
            "request was never on the phone) — unless it was confirmed "
            "elsewhere during the refusal and so taken back off the phone "
            "(see warnings)."),
        "dav_synced": _bool("ctag_bumped AND the collection is visible to "
                            "DavX5."),
        "warnings": _arr(_str(), "French, human-readable; empty when clean."),
        **_write_protocol_keys(),
    })


def _record_prescription_event_result() -> dict:
    """record_prescription_event's contract: the recorder result PLUS the
    answer that motivated the call — what the delay looks like NOW."""
    base = _entity_write_result({
        "type": _str(),
        "reference": _str(),
    }, dav=False, verb="recorded",
        extra=_dossier_etag(), optional=("dossier_etag",))
    base["properties"]["prescription_status"] = _str(
        "courante | interrompue | echue | imprescriptible | a_verifier — "
        "derived after the event.")
    base["properties"]["prescription_date_effective"] = _nstr(
        "YYYY-MM-DD; null when interrupted (until judgment) or not "
        "computable.")
    base["required"].extend(
        ["prescription_status", "prescription_date_effective"]
    )
    return base


def _write_result(verb: str, extra: Optional[dict[str, Any]] = None) -> dict:
    properties: dict[str, Any] = {
        verb: {"type": "boolean", "enum": [True]},
        "note": _written_note(),
        "ctag_bumped": _bool("Whether the DavX5 sync trigger fired. false = "
                             "the write COMMITTED but the phone will only "
                             "catch up on the next change; do not retry. "
                             "false when nothing was written."),
        "dav_synced": _bool("ctag_bumped AND the collection is visible to "
                            "DavX5 (a fermé/archivé dossier's is not)."),
        "warnings": _arr(_str(), "French, human-readable; empty when clean."),
        **_write_protocol_keys(),
    }
    if extra:
        properties.update(extra)
    return _obj(properties)


def _analyse_structure(*, headings: bool, nullable: bool = False) -> dict:
    """Where the zones of the « Théorie de la cause » stand
    (``utils/analyse_blocs.parse``), for a reader to aim an edit_analyse
    operation.

    *headings* — the READ variant (get_note) carries each heading's text:
    it is the note's own content, returned to a caller already reading
    the whole body. A WRITE result never does (edit_analyse): write
    payloads are stored 24 h in mcp_idempotency, and no write output
    echoes note text. *nullable* — get_note emits ``null`` for an
    ordinary note, so the key is always present."""
    zone: dict[str, Any] = {}
    if headings:
        zone["heading"] = _str(
            "The heading line's text, verbatim ('' for an entête without a "
            "level-1 title).")
    zone["chars"] = _int("Length of the zone's BODY (heading excluded).")
    zone["is_seed"] = _bool(
        "true = the body is still the template the note was seeded with.")
    bloc = _obj({
        "bloc": _str("The bloc letter, A to H — document order, a doubled "
                     "letter listed twice."),
        **zone,
    })
    schema = _obj({
        "ok": _bool("Every letter A–H exactly once AND in order."),
        "editable": _bool(
            "Every letter exactly once: a bloc operation can run. false = "
            "every operation is refused until the headings are restored "
            "(or the note is rewritten with `full`)."),
        "in_order": _bool("The letters appear in A…H order."),
        "missing": _arr(_str(), "Letters with no heading."),
        "duplicates": _arr(_str(), "Letters whose heading appears twice."),
        "entete": _obj(dict(zone), description="The text before bloc A."),
        "blocs": _arr(bloc),
        "interstitial_chars": _int(
            "Text under a level-1/2 heading that is NOT a bloc — kept by "
            "every bloc operation, never touched."),
        "content_length": _int("Stored length of the whole note."),
        "max_length": _int("The note's storage ceiling."),
        "remaining_chars": _int("max_length − content_length."),
    }, description=(
        "The théorie de la cause's zones. null for an ordinary note."
        if nullable else "The théorie de la cause's zones after this call."))
    if nullable:
        schema["type"] = ["object", "null"]
    return schema


def _task_status_effect() -> dict:
    """reopen_task's ``protocol_step_effect``: what the cascade DID, re-read
    from the documents after the write — never predicted."""
    return _obj({
        "checked": _bool(
            "false = no lookup ran (nothing was written, or the lookup "
            "itself failed — see `note`). An absent cascade is never "
            "confused with an unexamined one."),
        "linked_step_found": _bool(),
        "protocol_id": _str(),
        "step_id": _str(),
        "step_title": _str(),
        "step_status_before": _str(),
        "step_status_after": _str(
            "RE-READ after the write; '' when that re-read failed (then "
            "`note` says so)."),
        "protocol_status_before": _str(),
        "protocol_status_after": _str(
            "RE-READ after the write; '' when that re-read failed."),
        "protocol_reopened": _bool(
            "true = the protocol the cascade had closed is « actif » "
            "again: its deadlines are back in get_agenda. Meaningless when "
            "the re-read failed (`note`)."),
        "note": _str("French; empty when there is nothing to add."),
    })


# ── The registry ────────────────────────────────────────────────────────

def _phase_bulk_result(entity_type: str) -> dict:
    """The report a bulk phase reclassification returns.

    ``results`` mirrors the request's ``entries`` ONE-FOR-ONE AND IN ORDER,
    so a caller can zip the two. That ordering is the whole contract: a
    batch that applied some rows and refused others must be readable line by
    line, or it degrades into the silent partial success this codebase
    treats as the worst available failure.

    ``reason`` is the one conditional key — non-null only on a refusal — so
    it is typed and deliberately NOT required, per the output-schema rule
    that ``required`` lists only always-present keys.
    """
    label = "time entry" if entity_type == "time_entry" else "disbursement"
    return _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "entity_type": _str(f"Always « {entity_type} »."),
        "requested": _int("Rows received — always len(results)."),
        "applied": _int("Rows whose code actually changed."),
        "unchanged": _int(
            "Rows that already carried the requested code. NOTHING was "
            "written for these — no updated_at, no etag — which is what "
            "makes a reclassification pass safe to re-run."),
        "refused": _int(
            "Rows refused, each with its `reason`. A refusal never blocks "
            "the rest of the batch."),
        "results": _arr(
            _obj({
                "id": _str(f"The {label}'s id, echoed."),
                "outcome": {
                    "type": "string",
                    "enum": ["applied", "unchanged", "refused"],
                    "description": "What happened to THIS row.",
                },
                "reason": _nstr(
                    "French explanation; null unless outcome is « refused »."),
                "dossier_id": _nstr(
                    "null when the row could not be read — never '' , which "
                    "would read as « no dossier »."),
                "invoiced": {
                    "type": ["boolean", "null"],
                    "description": (
                        "null when the row could not be read: asserting « not "
                        "invoiced » about a row we never saw would be "
                        "inventing a fact. MAY be true — that is the point of "
                        "this tool."),
                },
                **_phase_pair(),
            }, required=["id", "outcome", "dossier_id", "invoiced",
                         "phase", "sous_phase", "phase_label",
                         "sous_phase_label"]),
            "One row per requested entry, SAME ORDER as the request.",
        ),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    })


# ── Lot 1b (L6) — the protocol writes ───────────────────────────────────

_PROTOCOL_TASK_SYNC = (
    "About the LINKED TASKS this write created or moved (the protocol and "
    "its steps are not synced to the phone; their tasks are). false = no "
    "task was written — or one was and its sync trigger failed, which a "
    "warning then says: do NOT retry."
)


def _protocol_write_entity(extra: Optional[dict[str, Any]] = None) -> dict:
    props: dict[str, Any] = {
        "id": _str("The protocol id."),
        "dossier_id": _str(),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "label": _str("The protocol title."),
        "date": _nstr("The start date, YYYY-MM-DD."),
        "protocol_type": _str("cq_simplifié | cs_ordinaire | conventionnel."),
        "status": _str("actif | suspendu | complété — as stored now."),
        "end_date": _nstr("YYYY-MM-DD — the latest step deadline."),
        "closed_by": _str(
            "« auto » (its last step's completion), « mcp », « web »…; '' "
            "while actif."),
        **_written_etag(),
    }
    if extra:
        props.update(extra)
    return _obj(props, optional=("etag",))


def _step_brief() -> dict:
    """A step in a protocol WRITE result: ids, dates, codes and the etag —
    never its notes or description (write results are stored 24 h)."""
    return _obj({
        "id": _str(),
        "order": _int(),
        "title": _str(),
        "deadline_date": _nstr("YYYY-MM-DD."),
        "deadline_offset_days": _nint(
            "Template offset in days after the start date; null on a "
            "custom step."),
        "status": _str("Derived from the deadline, as in list_protocol_steps."),
        "mandatory": _bool(),
        "deadline_locked": _bool("A CQ C.p.c. deadline: never editable."),
        "date_is_suggestion": _bool(
            "true = a CS template date not yet confirmed: a new start "
            "date moves it. CHANGING it (update_protocol_step "
            "deadline_date) confirms it; the connector cannot confirm it "
            "unchanged (the application's « Confirmer cette date » can)."),
        "linked_task_id": _nstr(),
        **_phase_pair(),
        "etag": _str(
            "The step's etag as stored after this write — pass it as "
            "update_protocol_step's `expected_etag`."),
    }, optional=("etag",))


def _step_write_entity() -> dict:
    return _written_entity({
        "protocol_id": _str(),
        "status": _str("The step status as stored now."),
        "mandatory": _bool("A C.p.c. template step."),
        **_written_etag(),
    }, optional=("etag",))


def _protocol_etag_key() -> dict[str, Any]:
    return {"protocol_etag": _str(
        "The PROTOCOL's etag re-read after this write (a step write "
        "restamps it) — update_protocol's `expected_etag`. '' when the "
        "re-read failed: re-read with list_protocol_steps.")}


def _protocol_sync_keys() -> dict[str, Any]:
    return {
        "ctag_bumped": _bool(_PROTOCOL_TASK_SYNC),
        "dav_synced": _bool(
            "ctag_bumped AND the dossier's collection is visible to DavX5 "
            "(a fermé/archivé dossier's is not)."),
    }


def _template_summary() -> dict:
    """A gabarit, as list_templates shows it — metadata and counts only.

    NEVER ``filename``/``original_filename`` (the original may embed a
    client's name: a template registered from a finished letter) nor
    ``storage_path``/``sha256``. The counts are RE-CLASSIFIED from the
    stored placeholders on every read, never the stored ``*_fields`` lists
    (stale on templates uploaded before the taxonomy changed)."""
    return _obj({
        "id": _str(),
        "name": _str(),
        "description": _str(),
        "category": _str("procédure | correspondance | autre — the "
                         "template's OWN taxonomy, never a document's."),
        "kind": _str("gabarit | note_honoraires | note."),
        "version": _int("Version of the file in force (1 = never replaced)."),
        "active": _bool(
            "true = THE template of its kind (note_honoraires or note), "
            "designated by the lawyer in the application — the one the "
            "application fills. Always false for kind « gabarit »."),
        "placeholder_count": _int(),
        "auto_count": _int(),
        "manual_count": _int(),
        "bloc_count": _int(),
        "flow_count": _int(
            "note / note_honoraires only: placeholders the kind's OWN flow "
            "fills (note.*, facture.* and the invoice rows); 0 for a "
            "gabarit."),
        "slots_required": _arr(_str(
            "dossier | client | adverse | destinataire — what the auto "
            "fields read.")),
        "validation_warnings": _arr(_str(
            "French, from the last upload: a field Word fragmented, which "
            "will not fill until retyped.")),
        **_stamps(),
    }, optional=_PROVENANCE_KEYS)


# ── Lot 2A (T7) — FILES: the document and folder edits ──────────────────

_CATEGORY_SOURCE_ENUM = ("juriste", "analyse", "mcp")


def _document_write_entity() -> dict:
    """A document AS STORED after update_document — filing fields only.

    Never ``notes_internes`` (the lawyer's text, which this tool cannot
    write and has no business echoing into a 24-hour idempotency record),
    never a filename, a path or a URL. ``etag`` is optional, the
    :func:`_written_etag` rule for a replayed result."""
    return _obj({
        "id": _str("The document's id."),
        "dossier_id": _str(),
        "display_name": _str(),
        "category": _str(),
        "category_source": {
            "type": "string", "enum": list(_CATEGORY_SOURCE_ENUM),
            "description": (
                "Who posed `category`: « mcp » = this connector, PRESUMED "
                "until the lawyer confirms it; « analyse » = derived from a "
                "recorded analysis; « juriste » = the lawyer (or a legacy "
                "document)."),
        },
        "category_presumee": _bool(
            "true = `category` is PRESUMED (category_source « analyse » or "
            "« mcp »), not the lawyer's determination."),
        "category_set_by_lawyer": _bool(
            "true = the lawyer CHOSE or CONFIRMED this category in the "
            "application (D18) — or it predates the marker and is not "
            "« autre », so it is held to be his: update_document never "
            "replaces it. false = presumed, or never chosen (a new upload's "
            "default, a generation's, a pre-marker document's « autre »)."),
        "tags": _arr(_str()),
        "document_date": _nstr("YYYY-MM-DD; null = not dated."),
        "folder_id": _nstr("null = the dossier root."),
        "etag": _str(
            "The document's etag AS STORED after this call — the next "
            "edit's expected_etag, without re-reading."),
    }, optional=("etag",))


def _folder_write_entity() -> dict:
    """A folder AS STORED after manage_folder. ``etag`` optional, as above."""
    return _obj({
        "id": _str("The folder's id."),
        "dossier_id": _str(),
        "name": _str(),
        "parent_folder_id": _nstr("null = at the dossier root."),
        "path": _nstr(
            "« Parent / Enfant ». null = the tree could not be re-read "
            "after the write (the write stands; list_documents with "
            "include_folders shows it)."),
        "system_role": _str(
            "« projets » | « portail » for a system folder (never "
            "renamed or moved); \"\" otherwise."),
        "etag": _str(
            "The folder's etag AS STORED after this call — the next "
            "rename/move's expected_etag."),
    }, optional=("etag",))


def _generated_folder() -> dict:
    """Where a generated or copied document landed (lot 2A, T8)."""
    return _obj({
        "id": _nstr("The folder's id; null = the dossier root."),
        "name": _str("« Projets » by default; \"\" at the dossier root."),
        "system_role": _str(
            "« projets » for the system folder the application files "
            "generated documents in; \"\" otherwise."),
    })


def _template_ref(*, nullable: bool = False) -> dict:
    """The template a generation filled — the version whose bytes it
    printed (models/doc_template.template_file_bytes)."""
    schema = _obj({
        "id": _str(),
        "name": _str(),
        "version": _int("The version of the file that was filled."),
    }, description=(
        "The active note-print template printed on; null for a copy."
        if nullable else "The gabarit filled."))
    if nullable:
        schema["type"] = ["object", "null"]
    return schema


# ── Lot 2A (T10) — TEMPLATES from a stored document ─────────────────────


def _template_leak_report(*, nullable: bool = False) -> dict:
    """The identifier check of a file taken from a stored document — ALWAYS
    performed, against that document's OWN dossier (unlike an upload
    ticket's, which runs only when the caller names a dossier).

    ``accepted_residues`` names each identifier the lawyer accepted: the
    dossier's own data, which the caller has just listed in
    ``accept_residual`` — echoed so the acceptance is visible where it
    lands, never the text around it."""
    schema = _obj({
        "performed": {
            "type": "boolean", "enum": [True],
            "description": (
                "Always true: a file taken from a dossier document is "
                "always checked against that document's dossier."),
        },
        "dossier_id": _str(
            "The source document's dossier — the one checked."),
        "accepted": _int(
            "Identifiers found in the FILE and accepted (accept_residual)."),
        "accepted_residues": _arr(_obj({
            "identifier": _str(
                "As a refusal names it — the dossier's own spelling."),
            "count": _int("Its occurrences in the file."),
            "where": _arr(_str(
                "French: corps, en-tête, pied de page, propriétés du "
                "document…")),
        }), "Each accepted identifier: it WILL print in every document "
            "generated from the template."),
        "accepted_in_name": _arr(_str(
            "An identifier accepted in the NEW template's name "
            "(create_template only; [] otherwise).")),
        "unused_accept": _int(
            "accept_residual entries matching nothing found."),
        "skipped": _int("Identifiers too short to check."),
        "parts_scanned": _int(),
    }, required=["performed", "dossier_id", "accepted", "accepted_residues",
                 "accepted_in_name", "unused_accept", "skipped",
                 "parts_scanned"])
    if nullable:
        schema["type"] = ["object", "null"]
        schema["description"] = "null on a metadata edit (no file read)."
    return schema


def _part_count_rows(description: str) -> dict:
    """A per-part tally: the package entry, its French label, the count."""
    return _arr(_obj({
        "part": _str("The package entry (word/header1.xml…)."),
        "where": _str("French: corps, en-tête, pied de page…"),
        "count": _int(),
    }), description)


def _templatize_preview_row() -> dict:
    """One substitution as preview_templatize counts it — counts, entry
    names and the caller's own field name; never the document's text, never
    the literal (its index names it)."""
    return _obj({
        "index": _int(
            "Its position in your list, from 0 — the French messages "
            "number from 1 (« n° 1 » is index 0)."),
        "placeholder": _str(
            'The field name, normalized; "" when invalid (see errors).'),
        "classification": {
            "type": ["string", "null"],
            "enum": ["auto", "manual", "passthrough", None],
            "description": (
                "auto = the application fills it; manual = prompted letter "
                "metadata; passthrough = left verbatim for Word (a misspelt "
                "catalog name lands here); null = invalid name."),
        },
        "expected_occurrences": _nint("Yours, when given."),
        "substituted": _int(
            "Occurrences it would replace (a text box counted once) — the "
            "value create_template's expected_occurrences must equal."),
        "matches_expected": {
            "type": ["boolean", "null"],
            "description": "null when you gave no expected_occurrences.",
        },
        "by_part": _part_count_rows("`substituted`, part by part."),
        "in_alternate_branches": _int(
            "Also replaced in a text box's second copy (mc:Fallback)."),
        "in_field_results": _int(
            "Left in place: in a Word field's result (Word regenerates it)."),
        "in_bound_controls": _int(
            "Left in place: in a content control bound to data."),
        "blocked_by_markup": _int(
            "Left in place: cut by a soft or non-breaking hyphen."),
        "in_existing_placeholders": _int(
            "Left in place: inside a {{…}} the document already holds."),
        "in_non_target_parts": _part_count_rows(
            "Left in place: footnotes, endnotes, comments, document "
            "properties — parts the fill engine never reads."),
        "fallback_consistent": _bool(
            "false = a text box's two copies differ — refused."),
    })


def _scrubbed_properties() -> dict:
    return {
        "type": ["array", "null"], "items": _str(),
        "description": (
            "The document properties emptied (scrub_properties): their "
            "NAMES, never their values; [] = nothing to empty; null = not "
            "asked (or a metadata edit); templatizing: always run, null "
            "only if it could not (see errors)."),
    }


# ── Lot 3b — BILL fragments ─────────────────────────────────────────────


def _billing_move_keys() -> dict[str, Any]:
    """What update_time_entry / update_expense say about a MOVE (lot 3b):
    always emitted, false/null on an edit that stays in its dossier."""
    return {
        "moved": _bool(
            "true = this call filed the row under another dossier (its "
            "`dossier_id` is the new one)."),
        "previous_dossier_id": _nstr(
            "The dossier it was in before a move; null when it did not move."),
    }


def _budget_version() -> dict:
    """One budget version as stored — lines by sub-code, its frozen rate."""
    return _obj({
        "id": _str(),
        "dossier_id": _str(),
        "version": _int("≥ 1, per dossier; the newest is the reference."),
        **_money("hourly_rate", "FROZEN into the version."),
        "note": _str("Assumptions, as written; '' when none."),
        "lines": _arr(_obj({
            "sous_phase": _str(),
            "phase": _str("Derived from the sub-code's prefix."),
            "phase_label": _str(),
            "sous_phase_label": _str(),
            "hours": _num(),
            **_money("frais", "Estimated disbursements."),
            **_money("fees", "hours × the version's rate."),
        })),
        "totals": _obj({
            "hours": _num(),
            **_money("fees"),
            **_money("frais"),
            **_money("total"),
        }),
        **_stamps(),
    }, optional=_PROVENANCE_KEYS)


def _budget_view() -> dict:
    """Budget vs actuals per phase — models.budget.build_budget_view."""
    return _obj({
        "rows": _arr(_obj({
            "phase": _str(),
            "libelle": _str("French phase label."),
            "budget_hours": _num(),
            **_money("budget"),
            "actual_hours": _num(),
            **_money("actual", "Billable time worked + every disbursement, "
                               "billed or not."),
            **_money("ecart", "budget − actual; negative = over budget."),
            "pct": {"type": ["number", "null"],
                    "description": "actual ÷ budget in DOLLARS, %; null "
                                   "when the phase has no envelope."},
            "level": {"type": "string",
                      "enum": ["none", "ok", "warn", "over"],
                      "description": "warn from 80 %, over past 100 %."},
        })),
        **_money("unphased", "Consumption carrying no phase code — shown "
                             "apart, never dropped."),
        **_money("budget_total"),
        **_money("actual_total"),
        "pct": {"type": ["number", "null"]},
        "level": {"type": "string", "enum": ["none", "ok", "warn", "over"]},
    })


def _invoice_write_entity(extra: dict[str, Any]) -> dict:
    """An invoice AS STORED after a BILL write — never its line text."""
    props: dict[str, Any] = {
        "id": _str(),
        "dossier_id": _str(),
        "dossier_file_number": _str("Snapshot at issuance."),
        "label": _str("The invoice number."),
        "invoice_number": _str(),
        "date": _nstr("YYYY-MM-DD."),
        "due_date": _nstr("YYYY-MM-DD."),
        "status": _str(),
        "status_label": _str(),
        **_money("total"),
        **_money("amount_due", "Frozen at issuance (total − retainer)."),
        **_written_etag(),
    }
    props.update(extra)
    return _obj(props, optional=("etag",))


# ── Lot 5b — ACCOUNTING fragments ───────────────────────────────────────
# A register row never carries a transit, an account number or a receipt's
# storage path (tests/test_mcp_accounting sweeps every real payload for
# them): the two bank-account fields are a payment credential, the path a
# capability.


def _nullable(schema: dict, description: str = "") -> dict:
    """*schema* (an object) or null — for a block a payload emits as null
    when it does not apply (the invoice of a dépense, a client of a bank
    fee)."""
    out = {**schema, "type": [schema.get("type", "object"), "null"]}
    if description:
        out["description"] = description
    return out


def _nmoney(key: str, description: str = "") -> dict[str, Any]:
    """The money pair, null when the figure does not exist."""
    return {
        f"{key}_cents": _nint(description),
        f"{key}_display": _nstr(),
    }


def _trust_register_row() -> dict:
    """A trust register entry AS STORED after an accounting write."""
    return _obj({
        "id": _str(),
        "dossier_id": _str("'' for an entry with no dossier (intérêts, "
                           "frais bancaires)."),
        "account_id": _str(),
        "sequence": _int("Continuous per account, never reused."),
        "date": _nstr("YYYY-MM-DD (date-only, never shifted)."),
        "direction": _str("recette or déboursé."),
        "purpose": _str(),
        "method": _str(),
        "status": _str("en_circulation, compensée or annulée."),
        "cleared_date": _nstr("YYYY-MM-DD; null while not cleared."),
        **_money("amount"),
        "counterparty": _str(),
        "client_id": _str("'' when none."),
        "client_name": _str(),
        "file_number": _str(),
        "reference": _str(),
        "description": _str(),
        "invoice_id": _str("The invoice a fee payment settles; '' otherwise."),
        "reverses_id": _str("On a correction: the entry it reverses; ''."),
        "reversed_by_id": _str("Its reversal; '' while standing."),
        "related_transaction_id": _str("A transfer's other leg; ''."),
        "balance_after_account_cents": _int(
            "FROZEN running balance of the account (journal view)."),
        "balance_after_client_cents": _int(
            "FROZEN running balance of the client (carte-client view)."),
        "created_via": _str(f"Path that recorded it: {_VIA_VOCABULARY}."),
        "updated_via": _str("Path of its last write."),
        "cleared_via": _str("Path that cleared it; '' while not cleared."),
        **_written_etag(),
    }, optional=("etag", "created_via", "updated_via"))


def _admin_register_props() -> dict[str, Any]:
    return {
        "id": _str(),
        "dossier_id": _str("The linked dossier; '' when none."),
        "account_id": _str(),
        "sequence": _int("The insertion order (the audit cursor)."),
        "date": _nstr("YYYY-MM-DD (date-only, never shifted)."),
        "kind": _str(),
        "kind_label": _str(),
        "direction": _str("recette or déboursé — implied by the kind."),
        "status": _str("en_circulation, compensée or annulée."),
        "cleared_date": _nstr("YYYY-MM-DD; null while not cleared."),
        **_money("amount", "Always positive — direction carries the sign."),
        **_money("net_amount", "The TPS/TVQ split, as stored."),
        **_money("gst_amount"),
        **_money("qst_amount"),
        "category": _str("A dépense's category; '' otherwise."),
        "counterparty": _str(),
        "description": _str(),
        "reference": _str(),
        "supplier_invoice_ref": _str(),
        "method": _str(),
        "dossier_file_number": _str(),
        "invoice_id": _str("The invoice an encaissement paid; '' otherwise."),
        "invoice_number": _str(),
        "trust_transaction_id": _str(
            "The trust fee payment this recette mirrors; '' otherwise — such "
            "an entry reverses only from its trust side."),
        "reverses_id": _str("On a correction: the entry it reverses; ''."),
        "reversed_by_id": _str("Its reversal; '' while standing."),
        "related_transaction_id": _str("A card payment's other leg; ''."),
        "has_receipt": _bool("A supporting document is attached (in the "
                             "application only)."),
        "revisions_count": _int("Corrections kept in its revision trail."),
        "created_via": _str(f"Path that recorded it: {_VIA_VOCABULARY}."),
        "updated_via": _str("Path of its last write."),
        "cleared_via": _str("Path that cleared it; '' while not cleared."),
    }


def _admin_register_row() -> dict:
    """An administration entry AS STORED after an accounting write."""
    return _obj({**_admin_register_props(), **_written_etag()},
                optional=("etag", "created_via", "updated_via"))


def _admin_ledger_row() -> dict:
    """A get_admin_ledger row: the entry, its lock and its running balance."""
    return _obj({
        **_admin_register_props(),
        **_provenance(),
        "created_at": _nstr("ISO-8601 Montréal."),
        "updated_at": _nstr("ISO-8601 Montréal."),
        "locked": _bool(
            "true = no longer editable (update_admin_entry refuses it): "
            "correct it by reverse_register_entry."),
        "lock_reason": _nstr(
            "Why: période_verrouillée (dated on or before the lock floor), "
            "écriture_verrouillée (compensée, annulée or part of a "
            "reversal), écriture_liée_facture, écriture_liée_fideicommis, "
            "paiement_carte_indivisible; null when editable."),
        **_nmoney("balance_after", (
            "Running ledger balance after this entry — null unless ONE "
            "account was read without a kind/status/category filter.")),
    }, optional=_PROVENANCE_KEYS)


def _invoice_payment_block() -> dict:
    """What a payment did to an invoice — null when no invoice was touched."""
    return _nullable(_obj({
        "id": _str(),
        "invoice_number": _str(),
        "status_before": _nstr(
            "Its status before THIS call; null on a replay "
            "(idempotent_replay: this call changed nothing — re-read "
            "get_invoice for its state now)."),
        "status_after": _str(
            "Its status after the write (on a replay: after the FIRST call)."),
        **_money("amount_paid", "The payment recorded on it."),
        **_money("balance", "amount_due − amount_paid."),
        "paid_in_full": _bool(),
    }))


def _client_balance_block() -> dict:
    return _nullable(_obj({
        **_money("book", "The client's balance in the register."),
        **_money("cleared", "What a déboursé may draw on (cleared funds)."),
    }), "The client's trust balances after this write; null without a "
        "client, and on a replay (re-read get_trust_balance).")


def _admin_account_row() -> dict:
    return _obj({
        "id": _str(),
        "name": _str(),
        "institution": _str(),
        "account_type": _str("opérations or carte_crédit."),
        "status": _str("actif or fermé."),
        "balance_label": _str("« Solde », or « Solde dû » for a card."),
        **_money("balance", "The figure shown beside balance_label (a "
                            "card's is what is owed, positive)."),
        **_money("ledger_balance", "Σ of the entries, in ledger sign."),
        "lock_floor": _nstr(
            "YYYY-MM-DD: the last completed reconciliation — no entry, "
            "clearing or reversal may be dated on or before it; null = "
            "never reconciled."),
        "last_reconciliation_date": _nstr("YYYY-MM-DD, or null."),
        "never_reconciled": _bool(),
        "reconciliation_overdue": _bool(),
    }, description="Never includes the transit or account number.")


def _reverse_branch(register: str, row: dict, extra: dict[str, Any]) -> dict:
    return _obj({
        "reversed": {"type": "boolean", "enum": [True]},
        "register": {"type": "string", "enum": [register]},
        "entity_type": _str(
            "trust_transaction or admin_transaction — the reversal."),
        "entity": row,
        "reversals": _arr(row, "Every reversal minted: one, or both legs of "
                               "a pair (a card payment, a transfer)."),
        "original": _obj({
            "id": _str(),
            "status_before": _nstr(
                "Its status before THIS call; null on a replay."),
            "status_after": _str("annulée, or compensée (kept)."),
        }),
        "invoices": _arr(_obj({
            "id": _str(),
            "invoice_number": _str(),
            "status_before": _nstr("null on a replay."),
            "status_after": _str(),
            **_money("amount_paid"),
            **_money("balance"),
            "paid_in_full": _bool(),
        }), "Each invoice whose recorded payment the reversal reduced."),
        **extra,
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    })


OUTPUT_SCHEMAS: dict[str, dict] = {
    "get_agenda": _obj({
        "window": _obj({
            "from": _str("YYYY-MM-DD, Montréal."),
            "to": _str(),
            "days_ahead": _int(),
        }),
        "hearings": _arr(_hearing_row(), "Upcoming, annulée excluded here."),
        "urgent_tasks": _arr(_task_row()),
        "urgent_protocol_steps": _arr(_step_row({
            "protocol_id": _str(),
            "protocol_title": _str(),
            "dossier_id": _str(
                "The dossier's UUID — what a write tool wants. Always "
                "present (\"\" only if the parent protocol carries none)."
            ),
            "dossier_file_number": _str(),
        })),
        "prescription_alerts": _arr(_obj({
            "dossier_id": _str(),
            "file_number": _str(),
            "title": _str(),
            "prescription_date": _nstr(
                "YYYY-MM-DD — the RAW computed date pour agir (provenance; "
                "never recomputed after an event)."),
            "prescription_date_effective": _nstr(
                "YYYY-MM-DD — the date the countdown actually runs on: "
                "the raw date, pushed later by any "
                "reconnaissance/suspension events. Null on an a_verifier "
                "row."),
            "prescription_status": _str(
                "courante | echue | a_verifier here (interrompue and "
                "imprescriptible rows are silenced out of the alerts). "
                "a_verifier = alerted but the delay could not be "
                "computed — verify at the source."),
            "days_remaining": _nint(),
            "last_action_date": _nstr(
                "Last juridical day ON OR BEFORE the prescription date — "
                "the real last day to act. INCLUSIVE: when the deadline "
                "already falls on a juridical day this EQUALS "
                "prescription_date (see last_action_differs); it is NOT "
                "the date an action was taken."
            ),
            "last_action_differs": _bool(
                "True only when a weekend/holiday pulls the last action "
                "day EARLIER than the prescription date — the only case "
                "worth surfacing to the reader."
            ),
            "droit_action_date": _nstr(
                "YYYY-MM-DD — start of the prescription period (the "
                "« droit d'action »), so the alert can be sanity-checked "
                "against the delay."
            ),
            "prescription_notes": _str(),
        })),
        "stats": _obj({
            "open_dossiers": _int(),
            "unbilled_hours": _num(),
            **_money("unbilled"),
            **_money("unbilled_expenses"),
            **_money("outstanding",
                     "Σ of the LIVE balance (amount_due − amount_paid) over "
                     "invoices in status envoyée or en_retard. A derived "
                     "figure, not a stored one: `amount_due` alone is frozen "
                     "at issuance and would overstate this by everything "
                     "already collected."),
        }),
    }),

    "list_dossiers": _list_envelope(
        _dossier_list_row(),
        extra={"next_cursor": _nstr(
            "Opaque continuation token — pass back as `cursor` for the "
            "next page; null on the last page. Minted from the last "
            "returned row, so a continuation never skips matches."
        )},
        extra_required=["next_cursor"],
    ),

    "get_dossier": _found_or_not(
        _obj({
            "found": _found(True),
            "dossier": _obj({
                # Base row… except clients/opposing_parties, which are
                # {id, name} OBJECTS here (strings in list_dossiers rows).
                "id": _str(),
                "file_number": _str(),
                "title": _str(),
                "status": _str(),
                "domaine": _str(),
                "domaine_label": _str(),
                "role": _str(),
                "tribunal": _str(),
                "court_file_number": _str(),
                "opened_date": _nstr(),
                "prescription_date": _nstr("The computed « date pour agir »."),
                "clients": _arr(_partie_ref()),
                "opposing_parties": _arr(_partie_ref()),
                "sommaire": _str(),
                "greffe_number": _str(),
                "juridiction_number": _str(),
                "competence": _str(),
                "palais_de_justice": _str(),
                "district_judiciaire": _str(),
                "is_administrative_tribunal": _bool(),
                "forum_type": _str("judiciaire | administratif | federal | prejudiciaire."),
                "mandate_type": _str(),
                "fee_type": _str(),
                "fee_notes": _str(),
                "closed_date": _nstr(),
                "action": _str("Taxonomy action code, e.g. REC-01."),
                "action_label": _str(),
                "action_precision": _str(),
                "delai": _str("The taxonomy's SUGGESTED delay, never computed."),
                "delai_types": _arr(_str(), "§4 tokens: PE/PA/D/DR/A/R/N/I/S/V/F."),
                "delai_types_label": _str(),
                "a_valider": _bool(),
                "delai_point_depart": _str(),
                "ref_delai": _str(),
                "ref_fondement": _str(),
                "avis": _arr(_obj({
                    "libelle": _str(),
                    "delai": _str(),
                    "sanction": _str(),
                    "conditionnel": _bool(),
                })),
                "prescription_type": _str(),
                "prescription_label": _str(),
                "droit_action_date": _nstr(),
                "date_avis": _nstr("Confirmed avis préalable date — manual."),
                "prise_action_date": _nstr(
                    "Date the recourse was filed / the limitation period "
                    "interrupted (art. 2892 C.c.Q.) — manual, LEGACY: reads "
                    "as an implicit interruption_depot event. When set, this "
                    "dossier is also dropped from get_agenda's "
                    "prescription_alerts: the deadline no longer looms."
                ),
                "prescription_events": _arr(_obj({
                    "id": _str(),
                    "type": _str(
                        "interruption_depot (art. 2892/2896) | "
                        "interruption_reconnaissance (art. 2898) | "
                        "suspension (art. 2904) | renonciation "
                        "(art. 2883)."),
                    "type_label": _str("French display label."),
                    "date": _nstr("YYYY-MM-DD."),
                    "end_date": _nstr(
                        "YYYY-MM-DD — suspensions only; null otherwise."),
                    "reference": _str(
                        "Free text: article, document, circumstance."),
                    "document_id": _str(
                        "Optional link to a documents record; empty when "
                        "none."),
                }), "The manually-recorded prescription events, "
                    "chronological. They drive prescription_status and "
                    "prescription_date_effective on the base row; the raw "
                    "prescription_date is NEVER recomputed from them."),
                "prescription_notes": _str(),
                "significations": _arr(_obj({
                    "id": _str(),
                    "partie_id": _str(
                        "A party ON this dossier — arts. 145/147 C.p.c. "
                        "delays run PER PARTY."),
                    "date": _nstr("YYYY-MM-DD — the service date."),
                    "mode": _str(
                        "personnelle | domicile | huissier | notification "
                        "| avocat | publication."),
                    "mode_label": _str("French display label."),
                    "huissier_id": _str(
                        "Optional contact id of the bailiff; empty when "
                        "none."),
                    "pv_document_id": _str(
                        "Optional link to the procès-verbal document; "
                        "empty when none."),
                    "superseded_by": _str(
                        "Id of the SIBLING signification that replaces "
                        "this one (a corrected second PV). The OPERATIVE "
                        "service for a party is the one nothing "
                        "supersedes."),
                    "confirmee": _bool(
                        "True once the procès-verbal is in hand."),
                }), "Service of process, chronological. Deadline "
                    "derivation (réponse per defendant) is not computed "
                    "yet — read the dates and modes as recorded."),
                **_stamps(),
                **_money("hourly_rate"),
                "flat_fee_cents": _nint("null when unset — never coerced to 0."),
                "flat_fee_display": _nstr(),
                "contingency_percent": {
                    "type": ["number", "null"],
                    "description": "Percent (e.g. 25.0); stored as basis points.",
                },
                "contingency_percent_display": _nstr(),
                "valeur_cents": _nint("Amount in dispute; null when unset."),
                "valeur_display": _nstr(),
                "valeur_classe": _nstr("Roman numeral I–IV, or null."),
                "analyse_note_id": _nstr(
                    "Id of the dossier's « Théorie de la cause » note — read "
                    "it with get_note, write it with edit_analyse. null "
                    "unless analyse_note_state is « present »."),
                "analyse_note_state": {
                    "type": "string",
                    "enum": ["present", "absent", "duplicate", "unreadable"],
                    "description": (
                        "absent = none yet (edit_analyse without operations "
                        "creates it); duplicate = several exist and every "
                        "write refuses until one remains; unreadable = the "
                        "lookup failed — NOT « absent »."),
                },
            }, optional=_PROVENANCE_KEYS),
            "summaries": _obj({
                "tasks": _model_summary("Model-owned task summary."),
                "hearings": _model_summary("Model-owned hearing summary."),
                "notes": _model_summary("Model-owned note summary ({total})."),
                "documents": _model_summary("Model-owned document summary."),
                "protocol": _model_summary("Model-owned protocol summary."),
                "time": _obj({
                    "total_hours": _num(),
                    "unbilled_hours": _num(),
                    **_money("total_billable"),
                    **_money("unbilled"),
                }),
                "expenses": _obj({**_money("total"), **_money("unbilled")}),
                "invoices": _obj({
                    "count": _int(),
                    **_money("total_invoiced"),
                    **_money("total_paid"),
                    **_money("total_outstanding"),
                }),
            }),
        }),
        {"dossier_id": _nstr("Echo of the selector used (one is null)."),
         "file_number": _nstr()},
    ),

    "list_tasks": _list_envelope(_task_row(), extra=_next_offset()),

    "list_hearings": _list_envelope(
        _hearing_row(),
        extra={
            "mode": {
                "type": "string",
                "enum": ["window", "serie", "bookings_pending"],
                "description": (
                    "window = the date window (default); serie = one "
                    "recurring chain whole; bookings_pending = the "
                    "Bookings requests awaiting a decision."),
            },
            "window": _obj({
                "from": _nstr("YYYY-MM-DD; null outside the window mode."),
                "to": _nstr("YYYY-MM-DD; null outside the window mode."),
            }),
            **_next_cursor("Hearings page oldest-first, so it advances "
                           "forward in time."),
        },
        extra_required=["mode", "window", "next_cursor"],
    ),

    "list_notes": _list_envelope(_obj({
        "id": _str(),
        "dossier_id": _str("Empty string for a « Général » note."),
        "dossier_file_number": _str("Live label, freshened from the dossier."),
        "dossier_title": _str("Live label, freshened from the dossier."),
        "title": _str(),
        "category": _str(),
        "pinned": _bool(),
        "is_analyse": _bool(
            "True = the dossier's single « Théorie de la cause » note "
            "(the Analyse sheet) — written only through edit_analyse; "
            "append_to_note and update_note refuse it."
        ),
        **_stamps(),
        "content_preview": _str("First 280 characters, plain text."),
    }, optional=_PROVENANCE_KEYS), extra={
        "scope": {
            "type": "string",
            "enum": ["general", "dossier", "cabinet"],
            "description": (
                "The EFFECTIVE scope searched — echoed so a reader never has "
                "to infer which corpus produced these rows."
            ),
        },
        **_next_cursor("Cabinet scope only; null in the other scopes, which "
                       "page with offset."),
        "dossier_status_matched": _nint(
            "How many dossiers the `dossier_status` filter matched; null "
            "when no such filter was asked for. Zero here explains an empty "
            "result — the filter selected nothing (or the dossier index "
            "could not be read) — rather than letting it read as « the firm "
            "holds no such record »."
        ),
        **_next_offset(),
    }, extra_required=["scope", "next_cursor", "dossier_status_matched"]),

    "get_note": _found_or_not(
        _obj({
            "found": _found(True),
            "note": _obj({
                "id": _str(),
                "dossier_id": _str("Empty string for a « Général » note."),
                "dossier_file_number": _str(),
                "dossier_title": _str(),
                "title": _str(),
                "content": _str("Full raw Markdown."),
                "category": _str(),
                "pinned": _bool(),
                "is_analyse": _bool(
                    "True = the dossier's single « Théorie de la cause » "
                    "note (the Analyse sheet) — written only through "
                    "edit_analyse; append_to_note and update_note refuse it."
                ),
                "structure": _analyse_structure(headings=True, nullable=True),
                **_stamps(),
            }, optional=_PROVENANCE_KEYS),
        }),
        {"note_id": _str()},
    ),

    "list_documents": _list_envelope(
        _obj({
            "id": _str(),
            "dossier_id": _str(),
            "dossier_file_number": _str("Live label, freshened."),
            "dossier_title": _str("Live label, freshened."),
            "display_name": _str(),
            "category": _str(),
            "category_source": {
                "type": "string",
                "enum": ["juriste", "analyse", "mcp"],
                "description": (
                    "Who posed `category`: « juriste » (the lawyer — or a "
                    "legacy document, which is read as his), « analyse » "
                    "(derived by a recorded analysis, still PRESUMED), "
                    "« mcp » (set by this connector, PRESUMED until the "
                    "lawyer confirms it in the application)."),
            },
            "category_set_by_lawyer": _bool(
                "true = the lawyer CHOSE or CONFIRMED this category (D18): "
                "update_document refuses to replace it. false = presumed, "
                "or nobody chose it (a new upload's default, a "
                "generation's, a pre-marker document's « autre »); any "
                "other pre-marker category reads true."),
            "file_type": _str("MIME type."),
            "file_size": _int("Bytes."),
            "file_size_display": _str(),
            "version": _int(),
            "folder_id": _nstr("null = dossier root."),
            "folder_path": _str(
                "« Parent / Enfant » resolved per row. \"\" means dossier "
                "root — OR cabinet scope, where breadcrumbs are not resolved "
                "at all (it would cost one query per dossier). Check "
                "`folder_id` to tell the two apart."),
            "folder_system_role": _nstr(
                "The role of the folder the document is filed in: "
                "« projets » (every generated document) or « portail » "
                "(portal intake) — the two SYSTEM folders, which cannot be "
                "renamed or moved; \"\" = an ordinary folder or the dossier "
                "root. null = NOT resolved (a document in a folder, in "
                "cabinet scope or when the dossier's folders could not be "
                "read) — never read null as « ordinary »."),
            "document_date": _nstr(
                "YYYY-MM-DD — the document's OWN date (PV, jugement…), "
                "manually entered; null on documents not yet dated. "
                "created_at is only the upload instant."),
            "resume": _str(
            "Le résumé de l'analyse; '' si non analysé. C'est le texte du "
            "MODÈLE."),
        "notes_internes": _str(
            "Le texte du JURISTE. Rien ne le réécrit — ni une analyse, ni "
            "une réanalyse."),
        "genere_depuis": _str(
            "Provenance d'un document produit par la machine (« Générée "
            "depuis la facture 2026-003-03 »); '' sinon."),
            "tags": _arr(_str()),
        # ── L'état de l'analyse documentaire ─────────────────────────
        # Aucun outil de lecture ne le disait, si bien qu'un appelant ne
        # pouvait pas savoir ce qu'il avait déjà qualifié. Étroit à
        # dessein : de quoi décider s'il reste du travail, pas de quoi
        # dispenser d'ouvrir le document.
        "analysee": _bool(
            "true = ce document porte une analyse. Lisez-le AVANT de "
            "relancer une qualification : une réanalyse écrit une "
            "nouvelle entrée au journal."),
        "sous_nature": _str("Code de la table fermée; '' si non analysé."),
        "nature_detectee": _str("Dérivée du code; '' si non analysé."),
        "famille": _str(
            "JUDICIAIRE | CORRESPONDANCE | PREUVE | CABINET | INDETERMINE; "
            "'' si non analysé."),
        "niveau_protection": _nint(
            "0 public … 3 secret professionnel. null si non analysé. Une "
            "réanalyse ne l'abaisse JAMAIS — seul l'avocat le peut, dans "
            "l'application."),
        "privileges": _arr(_str("Codes cumulés qui fondent le niveau.")),
        "analyse_confirmee": _bool(
            "true = l'avocat a confirmé ou corrigé l'analyse. false = elle "
            "reste PRÉSUMÉE."),
        "divergence_protection": _bool(
            "true = la dernière analyse concluait à un niveau PLUS BAS que "
            "celui déjà retenu; le plus élevé a été tenu et l'avocat doit "
            "trancher."),
        "category_presumee": _bool(
            "true = `category` est PRÉSUMÉE — posée par une analyse ou par "
            "ce connecteur (`category_source` « analyse » ou « mcp ») — et "
            "non une détermination de l'avocat."),
            **_stamps(),
        }, optional=_PROVENANCE_KEYS),
        # Present ONLY when the request carried folder_id — typed, never
        # required (kept for compatibility; the per-row folder_path is the
        # general resolver).
        extra={
            "folder_path": _str("Breadcrumb of the REQUESTED folder. "
                                "Only present when folder_id was given."),
            # Present ONLY with include_folders (dossier scope) — typed,
            # never required.
            "folders": _arr(_obj({
                "id": _str(),
                "name": _str(),
                "parent_folder_id": _nstr(
                    "null = at the dossier root. A parent that no longer "
                    "exists is kept as stored; the folder then shows at the "
                    "root of `path`."),
                "path": _str("« Parent / Enfant »; \"\" only for a folder in "
                             "a parent cycle (never created by the "
                             "application)."),
                "system_role": _str(
                    "« projets » | « portail » — a SYSTEM folder, never "
                    "renamed nor moved; \"\" = an ordinary folder."),
                "etag": _str(
                    "Concurrency token of the folder, regenerated by every "
                    "write to it. '' on a folder written before folders "
                    "carried one."),
            }, optional=("etag",)), "The dossier's WHOLE folder tree, "
                "depth-first by name. Only "
                "present when include_folders was true."),
            "folders_truncated": _bool(
                "true = the tree holds more folders than were returned. Only "
                "present with `folders`."),
            "scope": {
                "type": "string",
                "enum": ["dossier", "cabinet"],
                "description": "The EFFECTIVE scope searched.",
            },
            **_next_cursor("Cabinet scope only; null in dossier scope, which "
                           "pages with offset."),
            "dossier_status_matched": _nint(
                "How many dossiers the `dossier_status` filter matched; null "
                "when no such filter was asked for. Zero here explains an empty "
                "result — the filter selected nothing (or the dossier index "
                "could not be read) — rather than letting it read as « the firm "
                "holds no such record »."
            ),
            **_next_offset(),
        },
        extra_required=["scope", "next_cursor", "dossier_status_matched"],
    ),

    "list_templates": {
        # Root `type: object` BESIDE the anyOf — the wire schema demands it
        # (see _found_or_not).
        "type": "object",
        "anyOf": [
            _obj({
                "mode": {"type": "string", "enum": ["list"]},
                "items": _arr(_template_summary()),
                "count": _int("Number of items returned (post-truncation)."),
                "truncated": _bool(
                    "true when more matches exist than were returned."),
                **_next_offset(),
            }, required=["mode", "items", "count", "truncated"],
                description="No template_id: the list."),
            _obj({
                "mode": {"type": "string", "enum": ["detail"]},
                "found": _found(True),
                "template": _obj({
                    **_template_summary()["properties"],
                    "active_designated_at": _nstr(
                        "ISO-8601 Montréal: when the lawyer designated it "
                        "active; null when it is not the active one."),
                }, optional=_PROVENANCE_KEYS),
                "dossier": {
                    **_obj({"id": _str(), "file_number": _str(),
                            "title": _str()},
                           description="The dossier resolved against; null "
                                       "when none was given."),
                    "type": ["object", "null"],
                },
                "slots": _obj({
                    "client_id": _nstr(),
                    "adverse_id": _nstr(),
                    "destinataire_id": _nstr(),
                }, description="The party ids RETAINED (an omitted client or "
                   "adverse slot takes the dossier's only one); null = "
                   "empty slot. note / note_honoraires: only the slots their "
                   "own flow fills (client and adverse: never)."),
                "auto_fields": _arr(_obj({
                    "name": _str("As the template spells it."),
                    "resolved": _bool(
                        "true = the application has a value for it on these "
                        "slots. The VALUE is never returned."),
                    "slot": _nstr(
                        "dossier | client | adverse | destinataire — where "
                        "the value comes from; null for the firm and "
                        "today's date."),
                })),
                "unresolved_auto_fields": _arr(_str(
                    "Would print « [CHAMP MANQUANT : name] » — report the "
                    "missing data to the lawyer.")),
                "manual_fields": _arr(_obj({
                    "name": _str(),
                    "default": _str("Printed when left blank; '' = none, "
                                    "the field then prints « [À COMPLÉTER : "
                                    "name] »."),
                    "uppercase": _bool("The value is printed in capitals."),
                    "options": {
                        "type": ["array", "null"],
                        "items": _obj({
                            "label": _str(),
                            "value": _str("What a choice submits."),
                            "prints_nothing": _bool(
                                "true = the deliberate « no mention » "
                                "choice: prints NOTHING — unlike leaving the "
                                "field blank."),
                        }),
                        "description": "The closed list of choices; null = "
                                       "free text.",
                    },
                })),
                "blocs": _arr(_obj({
                    "name": _str("EXACT spelling, case included."),
                    "uppercase": _bool(),
                }), "Placeholders the application leaves verbatim, to be "
                    "written: free content, civilité, salutations, unknown "
                    "names."),
                "flow_fields": _arr(_str(), (
                    "note / note_honoraires only: filled by the kind's own "
                    "flow — create_document (source markdown for a note, "
                    "invoice_note for an invoice note) or the application's "
                    "own buttons — never by hand. [] for a gabarit.")),
                "versions": {
                    "type": ["array", "null"],
                    "items": _obj({
                        "version": _int(),
                        "current": _bool("The version in force."),
                        "installed_at": _nstr(
                            "ISO-8601 Montréal: when this file became the "
                            "template's; null when not recorded."),
                        "installed_via": _str(
                            "web | dav | mcp | cron | script; '' = not "
                            "recorded."),
                        "restored_from": _nint(
                            "The older version it restored; null otherwise."),
                        "file_size": _int("Bytes."),
                    }),
                    "description": "Recorded file versions, newest first; "
                                   "[] for a template whose file was never "
                                   "replaced since versions were recorded; "
                                   "null = the history could not be read.",
                },
                "versions_truncated": _bool(),
                "warnings": _arr(_str()),
            }, description="With template_id: the inventory."),
            _obj({
                "mode": {"type": "string", "enum": ["detail"]},
                "found": _found(False),
                "template_id": _str(),
            }, description="No template has this id — absence is data."),
        ],
    },

    "list_parties": _list_envelope(_obj({
        "id": _str(),
        "display_name": _str(),
        "type": _str(),
        "contact_role": _str(),
        "is_organization": _bool(),
        "city": _str(),
        **_stamps(),
    }, optional=_PROVENANCE_KEYS), extra=_next_offset()),

    "get_partie": _found_or_not(
        _obj({
            "found": _found(True),
            "partie": _obj({
                "id": _str(),
                "type": _str(),
                "contact_role": _str(),
                "display_name": _str(),
                "prefix": _str(),
                "first_name": _str(),
                "last_name": _str(),
                "organization_name": _str(),
                "trade_name": _str(),
                "governing_law": _str(),
                "language": _str(),
                "gender": _str(),
                "pronouns": _str(),
                "job_title": _str(),
                "job_role": _str(),
                "organization": _str(),
                "email": _str(),
                "email_work": _str(),
                "phone_home": _str("E.164."),
                "phone_home_display": _str(),
                "phone_cell": _str(),
                "phone_cell_display": _str(),
                "phone_work": _str(),
                "phone_work_display": _str(),
                "fax": _str(),
                "fax_display": _str(),
                "address": _address(),
                "work_address": _address(),
                "bar_number": _str(),
                "company_neq": _str(),
                "identity_verified": _str(),
                "identity_verified_date": _nstr(),
                "identity_verified_notes": _str("May be sensitive."),
                "conflict_check": _str(),
                "conflict_check_date": _nstr(),
                "conflict_check_notes": _str("May be sensitive."),
                **{
                    f"{field}_{key}": schema
                    for field in ("identity_verified", "conflict_check")
                    for key, schema in (
                        ("source", {
                            "type": "string", "enum": ["juriste", "mcp"],
                            "description": (
                                "Who decided the current status: « mcp » = "
                                "this connector (record_kyc_status); "
                                "« juriste » = the lawyer — also every "
                                "status recorded before provenance existed."),
                        }),
                        ("presumed", _bool(
                            "true = a decided status this connector "
                            "inscribed and the lawyer has not confirmed: "
                            "still OPEN in the coverage report. A decided "
                            "status with false is the lawyer's attestation "
                            "— record_kyc_status refuses to touch it.")),
                        ("confirmed_at", _nstr(
                            "ISO-8601 Montréal: when the lawyer confirmed "
                            "an inscription of this connector; null "
                            "otherwise.")),
                    )
                },
                "kyc_document_ids": _arr(_str()),
                "mandataires": _arr(
                    _obj({"id": _str(), "kind": _str(), "notes": _str()},
                         required=[]),
                    "Model-owned entries {id, kind, notes}.",
                ),
                "notes": _str(),
                **_stamps(),
            }, optional=_PROVENANCE_KEYS),
            "dossiers": _arr(_obj({
                "id": _str(),
                "file_number": _str(),
                "title": _str(),
                "status": _str(),
                "relation": _str("client, partie_adverse, or avocat (the contact is a party's lawyer on that dossier)."),
            })),
        }),
        {"partie_id": _str()},
    ),

    # Root type beside the anyOf — the Tool.outputSchema wire shape
    # requires it (see _found_or_not).
    "get_billing_snapshot": {"type": "object", "anyOf": [
        _obj({
            "scope": {"type": "string", "enum": ["global"]},
            "unbilled_hours": _num(),
            **_money("unbilled"),
            **_money("unbilled_expenses"),
            **_money("outstanding",
                     "Σ of the LIVE balance (amount_due − amount_paid) over "
                     "invoices in status envoyée or en_retard. A derived "
                     "figure, not a stored one: `amount_due` alone is frozen "
                     "at issuance and would overstate this by everything "
                     "already collected."),
            "by_dossier": _arr(_obj({
                "dossier_id": _str(),
                "file_number": _str(),
                "title": _str(),
                "unbilled_hours": _num(),
                **_money("unbilled_fees"),
                **_money("unbilled_expenses"),
            }), "Which dossiers hold the unbilled work (fees + "
                "disbursements), newest file first."),
            "by_dossier_truncated": _bool(
                "True when >200 unbilled rows exist — the breakdown may "
                "then under-count vs the exact aggregate totals."),
            "outstanding_invoices": _arr(_invoice_row()),
            "outstanding_invoices_truncated": _bool(),
        }, description="Firm-wide posture (no dossier_id given)."),
        _obj({
            "scope": {"type": "string", "enum": ["dossier"]},
            "found": _found(True),
            "dossier_id": _str(),
            "total_hours": _num(),
            "unbilled_hours": _num(),
            "invoice_count": _int(),
            **_money("total_billable"),
            **_money("unbilled_fees"),
            **_money("total_expenses"),
            **_money("unbilled_expenses"),
            **_money("total_invoiced"),
            **_money("total_paid"),
            **_money("total_outstanding"),
            "unbilled_time_entries": _arr(_obj({
                "id": _str(),
                "date": _nstr("YYYY-MM-DD."),
                "description": _str(),
                "hours": _num(),
                **_money("rate"),
                **_money("amount"),
            })),
            "unbilled_time_entries_truncated": _bool(),
            "unbilled_expenses_list": _arr(_obj({
                "id": _str(),
                "date": _nstr(),
                "description": _str(),
                "category": _str(),
                "taxable": _bool(),
                **_money("amount"),
            })),
            "unbilled_expenses_list_truncated": _bool(),
        }, description="One dossier's posture."),
        _obj({
            "found": _found(False),
            "dossier_id": _str(),
        }, description="Unknown dossier — absence is data, never zeros."),
    ]},

    "list_time_entries": _list_envelope(_obj({
        "id": _str(),
        "dossier_id": _str(),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "date": _nstr("YYYY-MM-DD."),
        "description": _str(),
        "hours": _num(),
        "billable": _bool("Non-billable time always carries amount 0."),
        "invoiced": _bool(),
        "invoice_id": _nstr("null until invoiced."),
        **_phase_pair(),
        **_stamps(),
        # After _stamps, so this description — the billing contract's own —
        # is the one declared.
        "created_via": _billing_created_via(),
        **_money("rate"),
        **_money("amount"),
    }, optional=_BILLING_ROW_OPTIONAL), extra=_next_cursor(), extra_required=["next_cursor"]),

    "list_expenses": _list_envelope(_obj({
        "id": _str(),
        "dossier_id": _str(),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "date": _nstr("YYYY-MM-DD."),
        "description": _str(),
        "category": _str(),
        "taxable": _bool(),
        "invoiced": _bool(),
        "invoice_id": _nstr("null until invoiced."),
        **_phase_pair(),
        **_stamps(),
        # After _stamps, so this description — the billing contract's own —
        # is the one declared.
        "created_via": _billing_created_via(),
        **_money("amount"),
    }, optional=_BILLING_ROW_OPTIONAL), extra=_next_cursor(), extra_required=["next_cursor"]),

    "list_invoices": _list_envelope(
        _invoice_row(), extra=_next_cursor(), extra_required=["next_cursor"]
    ),

    "get_invoice": _found_or_not(
        _obj({
            "found": _found(True),
            "invoice": _invoice_row({
            "dossier_title": _str("Snapshot at issuance."),
            "client_id": _str(),
            "notes": _str(),
            "payment_terms": _str(),
            "gst_rate_display": _str("e.g. « 5 % »."),
            "qst_rate_display": _str("e.g. « 9,975 % »."),
            **_money("subtotal_fees"),
            **_money("subtotal_expenses"),
            **_money("subtotal", "Fees + disbursements, before taxes."),
            **_money("gst_amount"),
            **_money("qst_amount"),
            **_money("retainer_applied"),
            **_money("line_items_total",
                     "Sum of the line amounts, recomputed here."),
            "subtotal_matches_line_items": _bool(
                "false = the stored subtotal and the sum of the lines "
                "disagree. Raise it; never silently re-add."),
            "line_items": _arr(_obj({
                "id": _str(),
                "type": _str("fee | expense."),
                "source_id": _nstr("The time entry or expense it came from."),
                "date": _nstr("YYYY-MM-DD."),
                "description": _str(
                    "VERBATIM as printed on the client's invoice — never "
                    "paraphrase it back."),
                "hours": {"type": ["number", "null"],
                          "description": "Fee lines only; null on a disbursement."},
                "rate_cents": {"type": ["integer", "null"],
                               "description": "Hourly rate; null on a disbursement."},
                "rate_display": _nstr(),
                "taxable": _bool(),
                **_money("amount"),
            })),
            "warnings": _arr(_str(), "French; empty when nothing is amiss."),
            # Lot 3b: what update_invoice needs, read here.
            "etag": _str(
                "Concurrency token of the stored invoice — update_invoice's "
                "expected_etag. '' on a legacy invoice that never had one."),
            "connector_transitions": _arr(_str(), (
                "The statuses update_invoice can set NOW (« annulée » = the "
                "void). Judged on this invoice alone: a payment standing in "
                "the registers is re-checked by the void itself.")),
            "void_reason": _nstr(
                "Why the invoice was voided (update_invoice); null unless "
                "annulée, '' when voided in the application."),
            "created_via": _str(
                f"Path that created the invoice: {_VIA_VOCABULARY}. '' = not "
                "recorded."),
            }, optional=("etag", "created_via")),
        }),
        {"invoice_id": _str("Echo of the id that was not found.")},
    ),

    # ── Lot 3b — BILL (reads) ───────────────────────────────────────────
    "preview_invoice": _obj({
        "dossier": _obj({
            "id": _str(), "file_number": _str(), "title": _str(),
            "status": _str("A closed dossier can still be billed."),
        }),
        "client": _obj({
            "id": _str("The dossier's FIRST client; '' when it has none."),
            "name": _str(),
        }),
        "sources": _arr(_obj({
            "id": _str(),
            "kind": {"type": "string", "enum": ["time_entry", "expense"]},
            "retained": _bool("true = it becomes a line of the invoice."),
            "reason": _nstr(
                "Why it cannot be billed (French) — introuvable, déjà "
                "facturée (and on which invoice), another dossier's, non "
                "facturable; null when retained."),
            "date": _nstr("YYYY-MM-DD; null when not retained."),
            "description": _nstr(
                "Prints VERBATIM on the invoice; null when not retained."),
            "hours": {"type": ["number", "null"],
                      "description": "Time entries only."},
            "taxable": {"type": ["boolean", "null"]},
            "amount_cents": {"type": ["integer", "null"]},
            "amount_display": _nstr(),
        })),
        "line_count": _int("Lines the invoice would carry."),
        **_money("subtotal_fees"),
        **_money("subtotal_expenses"),
        **_money("subtotal"),
        **_money("gst_amount"),
        **_money("qst_amount"),
        **_money("total", "Pass it as create_invoice's expected_total_cents "
                          "— with the SAME selection."),
        "number_prefix": _str(
            "The year's sequence (« 2026-F »). The number itself is drawn "
            "only when create_invoice commits."),
        "ready": _bool("false = create_invoice would refuse (see refusals)."),
        "refusals": _arr(_str(), "French; each blocks the creation."),
        "warnings": _arr(_str(), "French; true facts, never blocking."),
        "truncated": _bool(
            "all_unbilled: more sources than one invoice may carry — "
            "create_invoice refuses; name the sources instead."),
    }),

    "get_budget": _obj({
        "dossier_id": _str(),
        "has_budget": _bool(),
        "base_version": _int(
            "The version in force (0 = none) — create_budget_version's "
            "base_version."),
        "latest": {**_budget_version(), "type": ["object", "null"]},
        "versions": _arr(_obj({
            "id": _str(),
            "version": _int(),
            **_money("total", "Fees + disbursements of that version."),
            **_stamps(),
        }, optional=_PROVENANCE_KEYS), "Newest first."),
        "truncated": _bool("More versions than include_history returns."),
        "view": _budget_view(),
    }),

    "create_partie": _partie_write_result("created"),
    "update_partie": _partie_write_result("updated"),
    # ── Lot 4b — CONTACTS ────────────────────────────────────────────────
    "update_partie_mandataire": _partie_write_result("updated", extra={
        "action": {"type": "string", "enum": ["add", "update", "remove"]},
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged"],
            "description": (
                "« unchanged » = the representation already was so: NOTHING "
                "was written (no etag moved, no sync)."),
        },
        "mandataire": _obj({
            "partie_id": _str("The mandataire's contact id."),
            "kind": _str("The kind of representation (as stored, or as it "
                         "was on a remove)."),
            "has_notes": _bool("Whether it carries notes — never their text."),
        }),
        "mandataires_count": _int("Representations the contact has now."),
        "journaled": _bool(
            "remove: the detach is in the deletion journal (list_deletions, "
            "mandataire). false otherwise."),
    }),
    "record_kyc_status": _partie_write_result("recorded", extra={
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged"],
            "description": (
                "« unchanged » = the status was already so and no notes were "
                "given: NOTHING was written."),
        },
        "kyc": _obj({
            "check": {"type": "string", "enum": ["identity", "conflict"]},
            "field": _str("identity_verified | conflict_check."),
            "status_before": _str(),
            "status_after": _str("As stored now."),
            "source": {
                "type": "string", "enum": ["mcp"],
                "description": "The provenance this tool ALWAYS writes."},
            "presumed": _bool(
                "true = shown « … (présumé) », « inscrit par Claude le … — à "
                "confirmer » on the fiche, "
                "and still OPEN in the coverage report."),
            "confirmation_required": _bool(
                "true until the lawyer clicks « Confirmer » in the fiche — "
                "this connector never can."),
            "recorded_at": _nstr(
                "ISO-8601 Montréal: when the current decided status was "
                "inscribed; null for non_vérifié."),
            "notes_appended": _bool(
                "Your notes were appended under a dated line — never "
                "quoted back."),
        }),
    }),
    "update_time_entry": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « time_entry »."),
        "entity": _obj({
            "id": _str(),
            "dossier_id": _str(),
            "label": _str("The billing narrative."),
            "date": _nstr("YYYY-MM-DD."),
            "hours": _num(),
            "billable": _bool(),
            "invoiced": _bool("Always false — an invoiced entry is refused."),
            **_phase_pair(),
            **_money("rate"),
            **_money("amount", "Recomputed as hours x rate; 0 when not billable."),
            **_written_etag(),
        }, optional=("etag",)),
        **_billing_move_keys(),
        "warnings": _arr(_str()),
        **_write_protocol_keys(),
    }),

    "update_expense": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « expense »."),
        "entity": _obj({
            "id": _str(),
            "dossier_id": _str(),
            "label": _str(),
            "date": _nstr("YYYY-MM-DD."),
            "category": _str(),
            "taxable": _bool(),
            "invoiced": _bool("Always false — an invoiced one is refused."),
            **_phase_pair(),
            **_money("amount", "Stored verbatim; never recomputed."),
            **_written_etag(),
        }, optional=("etag",)),
        **_billing_move_keys(),
        "warnings": _arr(_str()),
        **_write_protocol_keys(),
    }),

    # ── Reclassement de phase (août 2026) ──────────────────────────────
    # The one write family whose entity may come back with
    # ``invoiced: true`` — the phase is on no invoice, so the wall that
    # freezes the money figures does not apply to it. The two `update_*`
    # schemas above still say « always false », and they still tell the
    # truth: their handlers refuse an invoiced row.

    "set_time_entry_phase": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « time_entry »."),
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged"],
            "description": (
                "« unchanged » = the entry already carried that exact code "
                "and NOTHING was written (no updated_at, no etag). A refusal "
                "is an error, never an outcome here."),
        },
        "entity": _obj({
            "id": _str(),
            "dossier_id": _str(),
            "label": _str("The billing narrative — echoed, never changed."),
            "date": _nstr("YYYY-MM-DD."),
            "hours": _num("Echoed unchanged: this tool cannot move it."),
            "billable": _bool("Echoed unchanged."),
            "invoiced": _bool(
                "MAY be true — the phase is correctable on a billed entry."),
            **_phase_pair(),
            **_money("rate", "Echoed unchanged."),
            **_money("amount", "Echoed unchanged — no figure moves here."),
            **_written_etag(),
        }, optional=("etag",)),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    }),

    "set_expense_phase": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « expense »."),
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged"],
            "description": (
                "« unchanged » = the disbursement already carried that code; "
                "nothing was written."),
        },
        "entity": _obj({
            "id": _str(),
            "dossier_id": _str(),
            "label": _str("Echoed, never changed."),
            "date": _nstr("YYYY-MM-DD."),
            "category": _str(
                "The DISBURSEMENT category — a different, orthogonal "
                "vocabulary from the litigation phase. Echoed unchanged."),
            "taxable": _bool("Echoed unchanged."),
            "invoiced": _bool("MAY be true."),
            **_phase_pair(),
            **_money("amount", "Echoed unchanged."),
            **_written_etag(),
        }, optional=("etag",)),
        "warnings": _arr(_str()),
        **_write_protocol_keys(),
    }),

    "set_time_entry_phase_bulk": _phase_bulk_result("time_entry"),
    "set_expense_phase_bulk": _phase_bulk_result("expense"),

    "import_invoice": _obj({
        "created": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « invoice »."),
        "entity": _obj({
            "id": _str("The stored id."),
            "dossier_id": _str(),
            "label": _str("The invoice number."),
            "invoice_number": _str("The number the previous system issued."),
            "date": _nstr("YYYY-MM-DD, the ORIGINAL date."),
            "status": _str("Always « brouillon »: import_invoice never "
                           "promotes it — update_invoice does."),
            "legacy_ref": _str(),
            **_money("subtotal_fees"),
            **_money("subtotal_expenses"),
            **_money("subtotal"),
            **_money("gst_amount"),
            **_money("qst_amount"),
            **_money("total", "Compare this to the paper invoice."),
        }),
        "line_count": _int("Line items, adjustment included."),
        "warnings": _arr(_str("French; empty when nothing is amiss.")),
        **_write_protocol_keys(),
    }, required=[
        "created", "entity_type", "entity", "line_count", "warnings",
        "idempotent_replay",
    ]),

    # ── Lot 3b — BILL (writes) ──────────────────────────────────────────
    "create_invoice": _obj({
        "created": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « invoice »."),
        "entity": _invoice_write_entity({
            **_money("subtotal_fees"),
            **_money("subtotal_expenses"),
            **_money("subtotal"),
            **_money("gst_amount"),
            **_money("qst_amount"),
        }),
        "line_count": _int(),
        "source_ids": _obj({
            "time_entry_ids": _arr(_str(), "Now invoiced — frozen."),
            "expense_ids": _arr(_str(), "Now invoiced — frozen."),
        }),
        "warnings": _arr(_str(), "French; the number is PERMANENT."),
        **_write_protocol_keys(),
    }),

    "update_invoice": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "mode": {"type": "string", "enum": ["draft", "status", "void"]},
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged"],
            "description": (
                "« unchanged » = the invoice already was so: NOTHING was "
                "written (no etag moved)."),
        },
        "entity_type": _str("Always « invoice »."),
        "entity": _invoice_write_entity({
            "previous_status": _str("The status before this call."),
            "void_reason": _nstr("null unless annulée."),
            **_money("amount_paid", "Recorded payment; 0 when none."),
            **_money("balance", "amount_due − amount_paid."),
        }),
        "changed_fields": _arr(_str(), "draft: the fields that changed."),
        "released_time_entry_ids": _arr(_str(), (
            "void: now unbilled — editable and billable again.")),
        "released_expense_ids": _arr(_str()),
        "foreign_source_ids": _arr(_str(), (
            "void: sources billed on ANOTHER invoice since — left alone.")),
        "missing_source_ids": _arr(_str(), (
            "void: sources that no longer exist — nothing to release.")),
        "connector_transitions": _arr(_str(), (
            "The statuses this tool can set next.")),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    }),

    "create_budget_version": _obj({
        "created": _bool("false with outcome « unchanged »."),
        "outcome": {
            "type": "string", "enum": ["created", "unchanged"],
            "description": (
                "« unchanged » = identical to the version in force: NOTHING "
                "was written."),
        },
        "entity_type": _str("Always « budget »."),
        "entity": _obj({
            "id": _str("The version's id."),
            "dossier_id": _str(),
            "label": _str("« vN »."),
            "version": _int(),
        }),
        "budget": _budget_version(),
        "diff": _obj({
            "added": _arr(_str(), "Sub-codes new in this version."),
            "changed": _arr(_str(), "Sub-codes whose hours or frais moved."),
            "removed": _arr(_str(), "Sub-codes the version in force had."),
        }),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    }),

    "create_dossier": _dossier_write_result("created"),
    "update_dossier": _dossier_write_result("updated"),
    # ── Lot 4b — DOSSIERS ────────────────────────────────────────────────
    "set_dossier_status": _dossier_write_result("updated", extra={
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged"],
            "description": (
                "« unchanged » = the dossier already had this status and "
                "closing date: NOTHING was written; its phone visibility was "
                "re-applied (a resync)."),
        },
        "status_before": _str("The stored status before this call."),
        "status_after": _str("The status as stored now."),
        "closed_date": _nstr("YYYY-MM-DD as stored now; null when open."),
        "closed_date_before": _nstr(
            "YYYY-MM-DD before this call — what a reopening erased."),
        "dav": _obj({
            "direction": {
                "type": "string", "enum": ["drain", "restore", "none"],
                "description": (
                    "drain = the collection was taken off the phone (closed "
                    "or archived); restore = put back (open)."),
            },
            "resources": _int(
                "How many tasks, notes and events the DavX5 write covered."),
            "ctag_bumped": _bool(
                "The collection's sync trigger fired with the last marker."),
            "complete": _bool(
                "false = the status IS written but the phone is not in step: "
                "call again with the SAME status."),
        }, description=(
            "What the status did to the phone's DavX5 collection of the "
            "dossier.")),
    }),
    "update_dossier_party": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "action": {
            "type": "string", "enum": ["update", "remove", "refresh_names"],
        },
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged", "partial"],
            "description": (
                "« unchanged » = nothing was written; « partial » "
                "(refresh_names) = at least one dossier was refused — the "
                "others were processed."),
        },
        "entity_type": _str("Always « dossier »."),
        "entity": _dossier_entity(),
        "party": _party_link(),
        "role_before": _str("The dossier-level derived role before."),
        "role_after": _str("…and as stored now (the gabarits cite it)."),
        "prescription_date": _nstr(
            "update / remove: the « date pour agir » as stored now."),
        "prescription_status": _str("update / remove."),
        "dossiers": _arr(_party_refresh_row(), "refresh_names: one row per dossier."),
        "applied": _int("refresh_names: dossiers written."),
        "unchanged": _int("refresh_names: dossiers already current."),
        "refused": _int("refresh_names: dossiers whose save was refused."),
        "warnings": _arr(_str(), "French; every side effect named."),
        **_write_protocol_keys(),
    }, required=["updated", "action", "outcome", "entity_type", "warnings",
                 "idempotent_replay"]),

    "get_import_audit": _found_or_not(
        _obj({
            "found": _found(True),
            "dossier": _dossier_list_row(),
            "completeness": _obj({
                "has_client": _bool(),
                "closed_without_closed_date": _bool(),
                "hourly_rate_is_default": _bool(
                    "The dossier still carries the model's default rate — on "
                    "a historical file, usually a rate that was never set."
                ),
                "legacy_ref": _str("'' when the record was not imported."),
            }),
            "time": _audit_block(),
            "expenses": _audit_block(),
            "invoices": _arr(_obj({
                "id": _str(),
                "invoice_number": _str(),
                "date": _nstr("YYYY-MM-DD."),
                "status": _str(),
                "legacy_ref": _str(),
                "line_count": _int(),
                "subtotal_matches_line_items": {
                    "type": ["boolean", "null"],
                    "description": (
                        "null when the line items could not be read — which "
                        "is NOT the same as a mismatch, and is why IMP-02 "
                        "stays silent on it."
                    ),
                },
                **_money("total"),
                **_money("line_items_total"),
            })),
            "findings": _arr(_obj({
                "code": _str("IMP-01 … IMP-07."),
                "severity": _str("manquement | signalement."),
                "label": _str(),
                "detail": _str(
                    "What to do, in French — the connector deletes nothing, "
                    "so a duplicate is removed in the application."
                ),
            })),
            "checks_skipped": _arr(_str(
                "Codes NOT run because the sources could not be read "
                "completely. A shortened report must never pass for a clean "
                "one."
            )),
            "truncated": _bool(),
        }),
        {
            "dossier_id": _nstr("Echo of the selector, when it was the id."),
            "file_number": _nstr("Echo of the selector, when it was the number."),
        },
    ),

    "get_reference_vocabulary": _obj({
        "kind": _str("Echo of the requested vocabulary."),
        "domaine": _str("Echo of the `actions` filter; '' when unfiltered."),
        "items": _arr(_obj({
            "code": _str("The value the write tools accept."),
            "label": _str("French display name."),
            "note": _str(
                "Whatever qualifies this row — for an action, the taxonomy's "
                "INDICATIVE delay (often '', which is deliberate: the source "
                "has no single clean period). '' when nothing qualifies it."
            ),
        })),
        "count": _int("Rows returned."),
        "truncated": _bool("More rows exist than were returned."),
    }),

    "find_imported": _obj({
        "legacy_ref": _str("Echo of the reference searched."),
        "matches": _arr(_obj({
            "entity_type": _str(
                "partie | dossier | time_entry | expense | invoice."
            ),
            "id": _str("UUIDv4 — pass it to the read tools verbatim."),
            "label": _str("Enough to recognise the record, never a full body."),
            "dossier_id": _nstr("null on a contact."),
        })),
        "count": _int(
            "0 means nothing bears this reference — a fact, not a read "
            "failure, which would have reported an error instead."
        ),
    }),

    "get_coverage_report": _obj({
        "scope": _obj({
            "status": _str("Dossier status the sweep covered."),
            "dossiers_examined": _int(),
            "checks_run": _arr(_str()),
            "checks_skipped": _arr(_str(
                "Codes NOT run — because their context could not be read, or "
                "because `checks` narrowed the sweep. A shortened report must "
                "never pass for a clean one."
            )),
        }),
        "summary": _obj({
            "dossiers_with_findings": _int(),
            "manquements": _int("Things the file is REQUIRED to have."),
            "signalements": _int("Worth a look; not a breach."),
            "by_code": _arr(_obj({
                "code": _str(),
                "label": _str(),
                "severity": _str("manquement | signalement."),
                "count": _int(),
            })),
        }),
        "items": _arr(_obj({
            "dossier_id": _str(),
            "file_number": _str(),
            "title": _str(),
            "status": _str(),
            "manquements": _int(),
            "signalements": _int(),
            "findings": _arr(_obj({
                "code": _str("Stable across runs — track a file by it."),
                "severity": _str(),
                "label": _str(),
                "detail": _str(
                    "French. Says what to do IN THE APPLICATION — the "
                    "connector only inscribes a PRESUMED identity or "
                    "conflict check, which stays open here until the lawyer "
                    "confirms it."
                ),
            })),
        }), "One entry per dossier WITH findings; clean files are omitted."),
        "count": _int(),
        "truncated": _bool(),
        **_next_cursor("Items are paged by file number."),
        "cross_scope_findings": _arr(_obj({
            "code": _str(),
            "severity": _str(),
            "label": _str(),
            "dossier_id": _str(),
            "file_number": _str(),
            "title": _str(),
            "status": _str(),
            "detail": _str(),
        }), "Findings on CLOSED dossiers, which the status filter could "
            "never surface — the ghost task on a closed file."),
        "data_completeness": _obj({
            "protocol_index_complete": _bool(
                "false = the protocol index could not be read, so the two "
                "protocol checks were SUPPRESSED rather than fired on every "
                "dossier at once."
            ),
            "kyc_checked": _bool(
                "false = the client contacts could not be read, so the "
                "deontological checks were suppressed. A client is NEVER "
                "reported unverified because a read failed."
            ),
            "kyc_reason": _str("French; empty when kyc_checked is true."),
        }),
    }),

    "list_deletions": _list_envelope(_obj({
        "id": _str(),
        "at": _nstr("ISO-8601 Montréal — the deletion instant."),
        # Lot 4b (step 4): the two LINK rows name something else in each
        # column — said here, where a reader of a row looks. Descriptions
        # only: no key, type or `required` moved.
        "entity_type": _str(
            "dossier_party / mandataire = a LINK detached, never a record: "
            "the contact stays."),
        "entity_id": _str(
            "The deleted entity; dossier_party: the contact; mandataire: "
            "the mandataire."),
        "dossier_id": _str("Empty when the entity had no dossier."),
        "title": _str(
            "Minimal snapshot — never the deleted content. mandataire: the "
            "name of the contact it REPRESENTED."),
        "status": _str(
            "The entity's status/category at deletion; dossier_party: the "
            "side it left; mandataire: the kind of representation."),
    })),

    "list_protocol_steps": _obj({
        "dossier_id": _str(),
        "has_active_protocol": _bool(),
        "protocols": _arr(_obj({
            "id": _str(),
            "title": _str(),
            "protocol_type": _str(),
            "status": _str(),
            "court": _str(),
            "dossier_tribunal": _str(
                "The dossier's current tribunal, for context."),
            "regime_mismatch": _bool(
                "True when the template's C.p.c. regime cannot govern "
                "this dossier's forum (e.g. a cq_simplifié — arts. 535.x "
                "— on a Superior Court file). Treat the tracked deadlines "
                "as suspect and raise it."),
            "start_date": _nstr(),
            "end_date": _nstr(),
            "notes": _str(),
            "closed_by": _str(
                "Who took the protocol out of « actif »: « auto » = the "
                "completion of its last step (reopening one of its steps "
                "reactivates it); « web » / « mcp » / … = a deliberate "
                "status change; '' = actif, or closed before this was "
                "recorded."),
            "closed_at": _nstr("ISO-8601 Montréal; null while actif."),
            "steps": _arr(_step_row()),
            **_stamps(),
            # After _stamps: the protocol etag is the one update_protocol
            # expects.
            "etag": _str(
                "Concurrency token of the stored protocol — pass it as "
                "update_protocol's `expected_etag`. Moved by every write to "
                "the protocol (a step edit restamps it too). '' on a legacy "
                "document that never carried one."),
        }, optional=_PROVENANCE_KEYS)),
    }),

    "compute_judicial_deadline": _obj({
        "start_date": _str(),
        "delay_days": _int(),
        "direction": {"type": "string", "enum": ["after", "before"]},
        "raw_date": _str("Uncorrected arithmetic landing date."),
        "deadline": _str("The art. 83 C.p.c. deadline (juridical day)."),
        "was_adjusted": _bool(),
        "adjustment_reason": _nstr("Human-readable; null when unadjusted."),
    }),

    "parse_court_file_number": _obj({
        "greffe_number": _nstr(),
        "juridiction_number": _nstr(),
        "palais_de_justice": _nstr(),
        "district_judiciaire": _nstr(),
        "point_de_service": {"type": ["boolean", "null"],
                             "description": "Itinerant circuit greffe."},
        "tribunal": _nstr(),
        "competence": _nstr(),
        "greffe_type": _nstr("GC / GP / GI."),
        "is_administrative": _bool(),
        "parse_error": _nstr("null on success."),
    }),

    "get_trust_balance": _found_or_not(
        _obj({
            "found": _found(True),
            "dossier_id": _str(),
            "file_number": _str(),
            "title": _str(),
            "has_trust": _bool(),
            **_money("total"),
            "by_client": _arr(_obj({
                "client_id": _str(),
                "client_name": _str(),
                **_money("book"),
                **_money("cleared"),
                **_money("in_transit"),
            }, description="book = register balance; cleared = available "
                           "for disbursement; in_transit = book − cleared.")),
        }),
        {"dossier_id": _nstr()},
    ),

    "list_trust_transactions": _obj({
        # Deliberately `transactions`, not the usual `items` — the register
        # is a domain document, not a generic listing.
        "transactions": _arr(_obj({
            "id": _str(),
            "sequence": _int("Continuous per account, never reused."),
            "date": _nstr("YYYY-MM-DD (date-only, never shifted)."),
            "file_number": _str(),
            "counterparty": _str(),
            "client_name": _str(),
            "purpose": _str(),
            "method": _str(),
            "direction": _str("recette or déboursé."),
            "status": _str(),
            "cleared_date": _nstr(),
            "reversed": _bool(),
            "balance_after_account_cents": _int(
                "FROZEN running balance (journal view); no display twin."),
            "balance_after_client_cents": _int(
                "FROZEN running balance (carte-client view)."),
            **_money("amount"),
        })),
        "count": _int(),
        "truncated": _bool(),
        **_next_cursor(
            "NULL on every filtered shape — only a bare account_id can be "
            "walked to the end (see the tool description)."
        ),
    }),

    "get_trust_snapshot": _obj({
        "accounts": _arr(_obj({
            "id": _str(),
            "name": _str(),
            "institution": _str(),
            "account_type": _str(),
            "last_reconciliation_date": _nstr(
                "YYYY-MM-DD period_end of THIS account's last completed "
                "reconciliation; null = never reconciled."),
            "never_reconciled": _bool(),
            "reconciliation_overdue": _bool(
                "Per-account: a month-end past its 30-day grace has no "
                "completed reconciliation covering it (accounts younger "
                "than their first due month-end are exempt)."),
            **_money("book_balance"),
            **_money("bank_balance"),
        }, description="Never includes the transit or account number.")),
        **_money("total_held"),
        "outstanding_count": _int(),
        **_money("outstanding_total"),
        "outstanding_cheques": _arr(_obj({
            "id": _str(),
            "account_id": _str(),
            "date": _nstr("YYYY-MM-DD — issue date; stale-cheque "
                          "monitoring reads this."),
            "reference": _str("Cheque number or reference."),
            "counterparty": _str(),
            "dossier_file_number": _str(),
            **_money("amount"),
        }), "Outstanding (en_circulation) cheques with their dates."),
        "outstanding_cheques_truncated": _bool(),
        "in_transit_count": _int(),
        **_money("in_transit_total"),
        "by_dossier": _arr(_obj({
            "dossier_id": _str(),
            "file_number": _str(),
            "title": _str(),
            "status": _str(),
            **_money("book_balance"),
            **_money("cleared_balance"),
        }), "Dossiers whose per-client trust map has entries — which "
            "files hold (or held) trust money."),
        "by_dossier_truncated": _bool(),
        "last_reconciliation_date": _nstr(
            "Most recent completed period_end across ALL accounts — see "
            "the per-account rows for the honest picture."),
        "reconciliation_overdue": _bool(
            "OR of the per-account flags — one compliant account no "
            "longer masks a never-reconciled sibling."),
        "reconciliation_never_performed": _bool(
            "Accounts exist and NO reconciliation has ever been "
            "completed, firm-wide."),
    }),

    "create_note": _write_result("created"),

    "append_to_note": _write_result(
        "appended", {"appended_chars": _int(
            "Length of the appended block, separator and provenance "
            "stamp included.")},
    ),

    "update_note": _entity_write_result(
        {
            "category": _str(),
            "pinned": _bool(),
            "content_length": _int(
                "Stored length AFTER this call (the provenance line "
                "included) — the body itself is never echoed."),
            **_written_etag(),
        },
        dav=True, verb="updated", relocates=True, entity_optional=("etag",),
        extra={
            "changed_fields": _arr(_str(), (
                "The fields this call actually changed. EMPTY = every value "
                "sent was already stored: nothing was written, no CTag moved "
                "(a safe replay).")),
            "moved": _bool("true = the note changed dossier (or went to or "
                           "from « Général »)."),
            "revision_id": _nstr(
                "Id of the revision keeping the REPLACED body (the note's "
                "revision history); null when the content did not change."),
            "linked_tasks_left_behind": _nint(
                "On a move: tasks linked to this note (RELATED-TO) that stay "
                "in their own dossier — jtx Board no longer shows the link. "
                "0 when none or when the note did not move; null = could not "
                "be counted."),
        },
    ),

    "edit_analyse": _entity_write_result(
        {
            "content_length": _int(
                "Stored length of the whole note after this call — the text "
                "itself is never echoed."),
            **_written_etag(),
        },
        dav=True, verb="edited", entity_optional=("etag",),
        extra={
            "mode": {
                "type": "string",
                "enum": ["created", "found", "blocs", "full", "unchanged"],
                "description": (
                    "created / found = no operation was asked: the note was "
                    "created pre-seeded, or already existed (nothing "
                    "written). blocs / full = the operations or the rewrite "
                    "were written. unchanged = they changed nothing: nothing "
                    "written, no CTag moved."),
            },
            "structure": _analyse_structure(headings=False),
            "blocs_changed": _arr(_obj({
                "bloc": _str("« entete » or a letter."),
                "mode": _str("replace | append."),
                "chars": _int("Characters sent for this bloc."),
            }), "One entry per operation applied, in order; empty otherwise."),
            "revision_id": _nstr(
                "Id of the revision keeping the replaced text (the note's "
                "revision history); null when nothing was written."),
        },
    ),

    "complete_task": _entity_write_result(
        {
            "status": _str("The status now stored."),
            "previous_status": _str("What it was before this call."),
            "completed_date": _nstr("ISO-8601 Montréal; null unless closed."),
            "is_overdue": _bool(),
        },
        dav=True,
        verb="completed",
        extra={
            "already_completed": _bool(
                "true = the task ALREADY carried the requested status and "
                "NOTHING was written (no cascade, no CTag). A scheduled job "
                "can replay safely on this."
            ),
            "protocol_step_effect": _obj({
                "checked": _bool(
                    "false = no lookup ran (a « Général » task has no "
                    "protocol). An absent cascade is never confused with an "
                    "unexamined one."
                ),
                "linked_step_found": _bool(),
                "protocol_id": _str(),
                "step_id": _str(),
                "step_title": _str(),
                "step_status_before": _str(),
                "step_status_after": _str(
                    "RE-READ from the document after the write, never "
                    "predicted: the model's sync swallows its own errors, so "
                    "a predicted value could be a lie."
                ),
                "protocol_closed": _bool(
                    "true = that was the last open step and the WHOLE "
                    "protocol closed. Its deadlines stop appearing in "
                    "get_agenda — see `warnings`."
                ),
                "note": _str("French; empty when there is nothing to add."),
            }),
        },
    ),

    "update_task": _entity_write_result(
        {
            "status": _str("Unchanged by this tool — shown for context."),
            "priority": _str(),
            "category": _str(),
            "phase": _str("Phase du litige (code, '' = non renseignée)."),
            "sous_phase": _str("Sous-code de phase ('' = non renseignée)."),
            **_written_etag(),
        },
        dav=True, verb="updated", relocates=True, entity_optional=("etag",),
        extra={
            "changed_fields": _arr(_str(), (
                "The fields this call actually changed. EMPTY = every value "
                "sent was already stored: nothing was written, no CTag moved "
                "(a safe replay).")),
            "moved": _bool("true = the task changed dossier (or went to or "
                           "from « Général »)."),
        },
    ),

    "reopen_task": _entity_write_result(
        {
            "status": _str("The status now stored."),
            "previous_status": _str("What it was before this call."),
            "completed_date": _nstr("ISO-8601 Montréal; null once reopened."),
            "is_overdue": _bool(),
            **_written_etag(),
        },
        dav=True, verb="reopened", entity_optional=("etag",),
        extra={
            "already_open": _bool(
                "true = the task ALREADY carried the requested status and "
                "NOTHING was written (no cascade, no CTag)."),
            "protocol_step_effect": _task_status_effect(),
        },
    ),

    "create_protocol": _obj({
        "created": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « protocol »."),
        "entity": _protocol_write_entity(),
        "steps": _arr(_step_brief(), "Every step, in order — empty for a "
                                     "conventionnel protocol."),
        "tasks_created": _int("Linked tasks created (0 unless asked)."),
        "tasks_linked": _int("Of those, the ones linked to their step."),
        "tasks_failed": _int("Steps whose task could not be created."),
        **_protocol_sync_keys(),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),

    "update_protocol": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « protocol »."),
        "entity": _protocol_write_entity({
            "previous_status": _str("The status before this call."),
        }),
        "changed_fields": _arr(_str(), (
            "The fields this call actually changed. EMPTY = every value "
            "sent was already stored: nothing was written.")),
        "recompute": _obj({
            "moved": _arr(_obj({
                "step_id": _str(),
                "title": _str(),
                "from": _nstr("The old deadline, YYYY-MM-DD."),
                "to": _nstr("The new deadline, YYYY-MM-DD."),
                "etag": _str("The moved step's new etag."),
                "linked_task_id": _nstr(),
                "task_outcome": {
                    "type": "string",
                    "enum": ["none", "aligned", "unchanged", "diverged",
                             "closed", "missing", "failed"],
                    "description": (
                        "What its linked task did: aligned = followed the "
                        "step; diverged = its own date, set by hand, left "
                        "alone; closed = terminée/annulée, left alone; "
                        "none = no linked task."),
                },
            }, optional=("etag",)), "Steps a new start_date moved."),
            "preserved": _arr(_obj({
                "step_id": _str(),
                "title": _str(),
                "reason": {
                    "type": "string", "enum": ["completed", "confirmed"],
                    "description": (
                        "completed = a past deadline is history; confirmed "
                        "= a CS date the lawyer confirmed."),
                },
            }), "Template steps a new start_date did NOT move."),
        }, description="Empty unless start_date changed."),
        "linked_tasks": _obj({
            "aligned": _int(), "unchanged": _int(), "diverged": _int(),
            "closed": _int(), "missing": _int(), "failed": _int(),
        }, description="Counts over the moved steps' linked tasks."),
        "open_steps": _int("Steps not « complété », after this call."),
        **_protocol_sync_keys(),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),

    "add_protocol_step": _obj({
        "created": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « protocol_step »."),
        "entity": _step_write_entity(),
        "step": _step_brief(),
        **_protocol_etag_key(),
        "linked_task_id": _nstr("The task created and linked, if any."),
        "task_created": _bool(),
        **_protocol_sync_keys(),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),

    "update_protocol_step": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « protocol_step »."),
        "entity": _step_write_entity(),
        "changed_fields": _arr(_str(), (
            "What this call changed (« status » for a status change). "
            "EMPTY = nothing was written: the values, or the state, were "
            "already stored.")),
        "date_confirmed_now": _bool(
            "true = this call confirmed a CS template date: a new start "
            "date will no longer move it."),
        "linked_task": _obj({
            "task_id": _nstr(),
            "outcome": {
                "type": "string",
                "enum": ["none", "aligned", "unchanged", "diverged",
                         "closed", "missing", "failed"],
                "description": (
                    "What the linked task did when the DEADLINE changed "
                    "(aligned = followed it; diverged = its own date, "
                    "left alone); none = the deadline did not change or "
                    "no task is linked."),
            },
            "ctag_bumped": _bool(),
        }),
        "status_change": _obj({
            "requested": _bool(
                "false = the call named no status (every other key is then "
                "empty). true with equal before/after statuses = the step "
                "already had it: nothing was written."),
            "step_status_before": _str(),
            "step_status_after": _str(
                "RE-READ after the write; '' when that failed (`note`)."),
            "task_id": _nstr(),
            "task_status_before": _str(),
            "task_status_after": _str("RE-READ after the write."),
            "task_sync": {
                "type": "string",
                "enum": ["none", "synced", "noop", "skipped_cancelled",
                         "skipped", "missing", "failed"],
                "description": (
                    "What the cascade did to the linked task: synced = "
                    "written; noop = already agreed; skipped_cancelled = "
                    "an annulée task, never touched; none = no linked "
                    "task, or no status change."),
            },
            "protocol_status_before": _str(),
            "protocol_status_after": _str("RE-READ after the write."),
            "protocol_closed": _bool(
                "true = that was the last open step: the WHOLE protocol "
                "closed and its steps left get_agenda."),
            "protocol_reopened": _bool(
                "true = the protocol its last step had closed is actif "
                "again."),
            "note": _str("French; empty when there is nothing to add."),
        }),
        **_protocol_etag_key(),
        **_protocol_sync_keys(),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),

    "create_task": _entity_write_result({
        "status": _str("Always « à_faire » — a created task is WORK, "
                       "never history."),
        "priority": _str(),
        "category": _str(),
        "phase": _str("Phase du litige (code, '' = non renseignée)."),
        "sous_phase": _str("Sous-code de phase ('' = non renseignée)."),
    }, dav=True),

    # The keys added in lot 1b (L7) are EMITTED on every call but declared
    # optional: an idempotency replay may return a result stored before the
    # deploy (the cache lives 24 h) — the _written_etag rule, which CLAUDE.md
    # states as « required gains only always-present keys ».
    "create_hearing": _entity_write_result({
        "hearing_type": _str(),
        "forum": _str("Derived from the type."),
        "all_day": _bool(),
        **_hearing_entity_extra(),
        **_written_etag(),
    }, dav=True, entity_optional=_HEARING_ENTITY_ADDED + ("etag",)),

    "create_hearing_series": _hearing_series_result(),

    "update_hearing": _entity_write_result(
        {
            "hearing_type": _str(),
            "forum": _str("Derived from the type."),
            "all_day": _bool(),
            **_hearing_entity_extra(),
            **_written_etag(),
        },
        dav=True, verb="updated", relocates=True, entity_optional=("etag",),
        extra={
            "changed_fields": _arr(_str(), (
                "The fields this call actually changed. EMPTY = every value "
                "sent was already stored: nothing was written, no CTag "
                "moved (a safe replay).")),
            "moved": _bool("true = the event changed dossier (or went to "
                           "or from « Général »)."),
            "detached": _bool("true = this call took the occurrence out of "
                              "its series."),
            "previous_serie_id": _str(
                "The chain it left when detached; '' otherwise."),
            "previous_status": _str("The status before this call."),
            "outlook_mirror": {
                "type": "string",
                "enum": ["follows", "removed", "not_mirrored", "unchanged"],
                "description": (
                    "What the 10-minute Outlook mirror of confirmed events "
                    "(when active; events 30 days back to 365 ahead) does "
                    "with this write: follows = its copy is created or "
                    "updated at the next cycle; removed = status annulée, "
                    "the copy is deleted; not_mirrored = a Bookings "
                    "rendez-vous, of which only the dossier or the notes "
                    "changed — neither is on its Outlook meeting (the "
                    "client's), which is never touched; unchanged = nothing "
                    "was written."),
            },
        },
    ),

    "decide_rendez_vous": _decide_rendez_vous_result(),

    "create_time_entry": _entity_write_result({
        "hours": _num(),
        "billable": _bool(),
        "phase": _str("Phase du litige (code, '' = non renseignée)."),
        "sous_phase": _str("Sous-code de phase ('' = non renseignée)."),
        **_money("rate"),
        **_money("amount"),
        **_written_etag(),
    }, dav=False, entity_optional=("etag",)),

    "create_expense": _entity_write_result({
        "category": _str(),
        "taxable": _bool(),
        "phase": _str("Phase du litige (code, '' = non renseignée)."),
        "sous_phase": _str("Sous-code de phase ('' = non renseignée)."),
        **_money("amount"),
        **_written_etag(),
    }, dav=False, entity_optional=("etag",)),

    "complete_dossier": _obj({
        "completed": {"type": "boolean", "enum": [True]},
        "entity_type": _str(),
        "dossier_id": _str(),
        "file_number": _str(),
        "title": _str(),
        "fields_set": _arr(_str(), "The fields actually filled."),
        "fields_already_identical": _arr(_str(),
            "Fields supplied with the value they already carry — "
            "skipped as harmless no-ops."),
        "prescription_date": _nstr(
            "The recomputed raw date pour agir after the fill."),
        "prescription_status": _str(),
        "warnings": _arr(_str()),
        **_write_protocol_keys(),
        **_dossier_etag(),
    }, optional=("dossier_etag",)),

    "record_signification": _entity_write_result({
        "partie_id": _str(),
        "mode": _str(),
        "confirmee": _bool(),
    }, dav=False, verb="recorded",
        extra=_dossier_etag(), optional=("dossier_etag",)),

    "record_prescription_event": _record_prescription_event_result(),

    # ── Lecture du contenu d'un document + analyse documentaire ─────────

    "get_document_text": {
        # Root type BESIDE the anyOf — the wire-mandated shape (see
        # _found_or_not). Three branches, enum-discriminated: readable,
        # honestly-unreadable, not-found.
        "type": "object",
        "anyOf": [
            _obj({
                "found": _found(True),
                "readable": {"type": "boolean", "enum": [True]},
                "document_id": _str(),
                "display_name": _str(),
                "file_type": _str("MIME type as stored."),
                "pagination_unit": {
                    "type": "string",
                    "enum": ["page", "segment"],
                    "description": "PDF pages, or computed .docx segments.",
                },
                "page_count": _int("Total units in the document."),
                "pages": _arr(_obj({
                    "page": _int("1-based unit number."),
                    "text": _str("Extracted text; empty when has_text is "
                                 "false."),
                    "has_text": _bool(
                        "false = NO text layer on this unit (scan, image "
                        "page) — never « the page is blank on paper », and "
                        "nothing was OCR'd."),
                    "page_truncated": _bool(
                        "true = this unit alone overflowed the per-call "
                        "ceiling and was cut; its tail is not retrievable "
                        "through this tool."),
                })),
                "pages_without_text": _arr(
                    _int(),
                    "Units of THIS response with no text layer — the "
                    "scanned-document signal (window-scoped, not "
                    "document-wide)."),
                "truncated": _bool(
                    "true = the requested window was cut short by the "
                    "per-call ceiling."),
                "next_page": _nint(
                    "Resume here with page_range; null when the document "
                    "is exhausted."),
                "warnings": _arr(_str(), "Machine-stable tokens."),
            }),
            _obj({
                "found": _found(True),
                "readable": {"type": "boolean", "enum": [False]},
                "document_id": _str(),
                "file_type": _str(),
                "reason": {
                    "type": "string",
                    "enum": [
                        "too_large", "unsupported_type", "encrypted",
                        "invalid_pdf", "invalid_docx", "download_failed",
                        "no_storage_path",
                    ],
                    "description": "Why the content cannot be extracted.",
                },
                "file_size_display": _str("Human-readable size."),
                "message": _str("French explanation, incl. what to do "
                                "instead."),
            }, description="The document exists but its content cannot be "
                           "extracted — said honestly, never faked."),
            _obj({
                "found": _found(False),
                "document_id": _str(),
            }, description="No such document — absence is data."),
        ],
    },


    "record_document_analysis": {
        "type": "object",
        "properties": {
            "recorded": {"type": "boolean"},
            "document_id": {"type": "string"},
            "display_name": {"type": "string"},
            "category": {"type": "string"},
            "category_source": {"type": "string"},
            "analyse": {"type": "object"},
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        # `required` ne porte que ce qui est TOUJOURS présent : un refus
        # n'a ni catégorie ni provenance à rapporter, rien n'étant stocké.
        "required": ["recorded", "document_id", "analyse", "warnings"],
    },

    # ── Lot 2A (T7) — FILES ─────────────────────────────────────────────
    "update_document": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « document »."),
        "entity": _document_write_entity(),
        "changed_fields": _arr(_str(), (
            "The fields this call changed; [] = every value sent was "
            "already stored, and nothing was written.")),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),
    "move_documents": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "dossier_id": _str(),
        "target": _obj({
            "folder_id": _nstr("null = the dossier root."),
            "path": _str("« Parent / Enfant »; \"\" = the dossier root."),
            "system_role": _str(
                "« projets » | « portail » when the target is a system "
                "folder (filing INTO one is allowed); \"\" otherwise."),
        }),
        "requested": _int("Ids received — always len(results)."),
        "moved": _int("Rows refiled by this call."),
        "unchanged": _int(
            "Rows already in the target: NOTHING was written for them, "
            "which is what makes a repeat safe."),
        "refused": _int("Rows refused, each with its `reason`."),
        "results": _arr(
            _obj({
                "document_id": _str("The id, echoed."),
                "outcome": {
                    "type": "string",
                    "enum": ["moved", "unchanged", "refused"],
                    "description": "What happened to THIS row.",
                },
                "reason": _nstr(
                    "French; null unless outcome is « refused »."),
                "previous_folder_id": _nstr(
                    "Where the document was filed before; null = the "
                    "dossier root, or a refused row (never read)."),
                "etag": _nstr(
                    "The document's etag as stored after this call; null "
                    "on a refused row."),
            }, optional=("etag",)),
            "One row per requested id, SAME ORDER as the request.",
        ),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),
    "manage_folder": _obj({
        "action": {
            "type": "string", "enum": ["create", "rename", "move"],
            "description": "The action asked.",
        },
        "outcome": {
            "type": "string",
            "enum": ["created", "reused", "renamed", "moved", "unchanged"],
            "description": (
                "« reused » = create with if_exists « reuse » found the "
                "name taken and returned that folder, untouched; "
                "« unchanged » = the folder already had that name or "
                "parent, nothing written."),
        },
        "entity_type": _str("Always « folder »."),
        "entity": _folder_write_entity(),
        "changed_fields": _arr(_str(), (
            "The folder fields this call set or changed; [] when nothing "
            "was written.")),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),

    # ── Lot 2A (T8) — FILES: new Word documents, never a changed one ────
    # Names and counts only — never a value the fill resolved, never the
    # text written (the result is kept 24 h in mcp_idempotency), never a
    # filename, a path or a URL.
    "fill_gabarit": _obj({
        "created": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « document »."),
        "entity": _document_write_entity(),
        "document_id": _str("The NEW document — same as entity.id."),
        "display_name": _str("« REF - YYYY-MM-DD - Projet Nom »."),
        "folder": _generated_folder(),
        "gabarit": _template_ref(),
        "fields": _obj({
            "auto_resolved": _int(
                "Fields the application filled from the dossier, its "
                "parties, the firm and the date."),
            "auto_missing": _arr(_str(), (
                "Auto fields with no data: printed « [CHAMP MANQUANT : "
                "name] » — a gap to report to the lawyer.")),
            "manual_missing": _arr(_str(), (
                "Manual fields with no value nor default: printed « [À "
                "COMPLÉTER : name] ».")),
            "blocs_filled": _arr(_str(), "The blocs this call wrote."),
            "blocs_demoted": _arr(_str(), (
                "Markdown blocs whose paragraph could not take Word "
                "formatting: printed as plain text, Markdown sigils "
                "visible.")),
            "blocs_left_verbatim": _arr(_str(), (
                "Placeholders still literal « {{name}} » in the document — "
                "the blocs nobody wrote, and any field Word fragmented — for "
                "the lawyer to complete in Word.")),
        }),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),
    "create_document": _obj({
        "created": _bool(
            "false only for source invoice_note with `reused` true: nothing "
            "was created."),
        "source": {
            "type": "string", "enum": ["markdown", "copy", "invoice_note"],
            "description": "What the document was made from.",
        },
        "reused": _bool(
            "invoice_note: true = a note identical to what would print was "
            "already filed — returned, NOTHING written. Always false for "
            "markdown and copy."),
        "invoice": {
            **_obj({
                "id": _str(),
                "invoice_number": _str(),
                "status": _str(),
            }),
            "type": ["object", "null"],
            "description": "invoice_note: the invoice printed; null otherwise.",
        },
        "entity_type": _str("Always « document »."),
        "entity": _document_write_entity(),
        "folder": _generated_folder(),
        "template": _template_ref(nullable=True),
        "source_document_id": _nstr(
            "copy: the document copied; null otherwise."),
        "protection": {
            **_obj({
                "niveau_protection": _int(
                    "0 public … 3 secret professionnel — inherited from the "
                    "source, PRESUMED until the lawyer qualifies the copy; "
                    "an analysis of the copy can only keep or raise it."),
                "label": _str("The level's French label."),
                "privileges": _arr(_str()),
            }, description=(
                "copy: the protection the copy inherited from its source; "
                "null when the source carried none (and always for "
                "markdown).")),
            "type": ["object", "null"],
        },
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),

    # ── Lot 2A (T9) — FILES: the upload ticket (plan D4) ────────────────
    # `upload_url` is the ONE capability URL any output carries — the
    # documented exception, allowlisted by name in
    # tests/test_mcp_framework_guards and by value in
    # tests/test_mcp_output_schemas; never stored (the tool's persist hook
    # strips it from mcp_idempotency). Nothing here names a file name, a
    # storage path or an MD5.
    "begin_upload": _obj({
        "opened": {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « upload_ticket »."),
        "entity": _obj({
            "id": _str("The ticket's id — same as ticket_id."),
            "dossier_id": _str(
                "The dossier bound to the ticket; \"\" for a gabarit with "
                "no source dossier."),
        }),
        "ticket_id": _str("Pass it to finalize_upload."),
        "purpose": {
            "type": "string", "enum": ["document", "gabarit"],
            "description": "What the file will become.",
        },
        "upload_url": _str(
            "A resumable-upload session URI, WRITE-ONLY: PUT the exact bytes "
            "to it, in one request, from your code sandbox. A capability — "
            "use it only in that code, never repeat it to the user. A replay "
            "of this call returns a FRESH one for the same ticket."),
        "method": {"type": "string", "enum": ["PUT"]},
        "headers": _obj({
            "Content-Type": _str("The type the file is stored under."),
            "Content-Length": _str("The declared size, in bytes."),
        }, description="Send these headers with the PUT, and no other."),
        "max_bytes": _int(
            "The declared size: the service refuses any byte beyond it."),
        "expires_at": _str(
            "ISO-8601, Montréal: finalize_upload before this instant. A later "
            "upload is never filed and is erased automatically."),
        "instructions": _str("What to do next, in plain words."),
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }),
    "finalize_upload": {
        "type": "object",
        "anyOf": [
            _obj({
                "finalized": {"type": "boolean", "enum": [True]},
                "purpose": {"type": "string", "enum": ["document"]},
                "ticket_id": _str(),
                "entity_type": {"type": "string", "enum": ["document"]},
                "entity": _document_write_entity(),
                "file": _obj({
                    "size_bytes": _int(),
                    "file_type": _str(
                        "The type the bytes were recognised as (sniffed, "
                        "never the declared one)."),
                }),
                "already_finalized": _bool(
                    "true = this ticket's file had ALREADY been filed (a "
                    "repeat, or an interrupted finalization completed) — "
                    "this call filed nothing new."),
                "warnings": _arr(_str(), "French; empty when clean."),
                **_write_protocol_keys(),
            }, description="purpose document: the NEW document."),
            _obj({
                "finalized": {"type": "boolean", "enum": [True]},
                "purpose": {"type": "string", "enum": ["gabarit"]},
                "ticket_id": _str(),
                "entity_type": {"type": "string", "enum": ["template"]},
                "entity": _template_summary(),
                "mode": {
                    "type": "string", "enum": ["create", "replace"],
                    "description": "What the ticket was opened for.",
                },
                "replaced_version": _nint(
                    "replace: the version this file replaced — KEPT, "
                    "restorable in the application; null for a creation, or "
                    "when the file was identical to the version in force "
                    "(nothing installed)."),
                "leak_scan": {
                    **_obj({
                        "performed": _bool(
                            "false = the ticket DECLARED aucun_dossier_source "
                            "(no source dossier): NOTHING was checked."),
                        "accepted": _int(
                            "Identifiers found and accepted (accept_residual)."),
                        "unused_accept": _int(
                            "accept_residual entries matching nothing found."),
                        "skipped": _int(
                            "Identifiers too short to check."),
                        "parts_scanned": _int(),
                    }),
                    "type": ["object", "null"],
                    "description": (
                        "The identifier check of the source dossier; null "
                        "when already_finalized (the first report is not "
                        "kept)."),
                },
                "scrubbed_properties": {
                    "type": ["array", "null"], "items": _str(),
                    "description": (
                        "The document properties emptied (scrub_properties): "
                        "their NAMES, never their values; [] = nothing to "
                        "empty; null = not asked, or already_finalized."),
                },
                "already_finalized": _bool(
                    "true = this ticket's file had ALREADY been filed — this "
                    "call installed nothing new."),
                "warnings": _arr(_str(), "French; empty when clean."),
                **_write_protocol_keys(),
            }, description="purpose gabarit: the template as stored."),
        ],
    },
    # Lot 2B — the templatize preview (a READ). Every key is emitted on
    # every call, null where it does not apply: all required.
    "preview_templatize": _obj({
        "document_id": _str(),
        "dossier_id": _str(
            "The source document's dossier — the one whose identifiers were "
            "checked."),
        "source_blockers": _arr(_obj({
            "code": _str(
                "tracked_changes | comments | strict_ooxml | namespace | "
                "encoding | malformed_xml | too_complex | too_much_text | "
                "duplicate_entry | unreadable."),
            "message": _str("French: what to do in Word."),
            "where": _arr(_str("French label of a part concerned.")),
        }), "Reasons the SOURCE cannot be templatized at all; [] = none."),
        "substitutions": _arr(_templatize_preview_row(),
                              "One row per substitution, in your order."),
        "result_placeholders": {
            **_obj({
                "placeholder_count": _int(),
                "auto_count": _int("Fields the application fills itself."),
                "manual_count": _int("Short letter metadata, prompted."),
                "passthrough_count": _int(
                    "Left verbatim, to complete in Word."),
                "fragmented_count": _int(
                    "Fields Word split across runs — they will not fill."),
            }),
            "type": ["object", "null"],
            "description": (
                "The would-be template's fields; null when no result could "
                "be computed (a source blocker, or an output check that "
                "failed — see errors)."),
        },
        "leak_scan": {
            **_obj({
                "residues": _arr(_obj({
                    "identifier": _str(
                        "The dossier's own spelling — or your literal, "
                        "exactly as you sent it."),
                    "count": _int(),
                    "where": _arr(_str("French label of a part.")),
                    "origin": {
                        "type": "string", "enum": ["dossier", "substitution"],
                        "description": (
                            "dossier = an identifier of the source dossier; "
                            "substitution = a literal of yours that would "
                            "survive somewhere."),
                    },
                }), "What would remain: create_template refuses on each "
                    "unless the lawyer accepts it (accept_residual)."),
                "skipped": _int("Identifiers too short to check."),
                "parts_scanned": _int(),
            }),
            "type": ["object", "null"],
            "description": (
                "The identifier check of the would-be result; null when it "
                "could not run (see errors)."),
        },
        "scrubbed_properties": _scrubbed_properties(),
        "ready_to_create": _bool(
            "true = no blocker, no error, every substitution found at least "
            "once, and nothing would remain: create_template with these "
            "substitutions, each expected_occurrences = its `substituted`, "
            "passes the file checks (its `name` is checked there)."),
        "errors": _arr(_str(
            "French: why create_template would refuse — an invalid "
            "substitution, a count that differs from yours, a text box "
            "whose two copies differ, a check that could not run.")),
        "warnings": _arr(_str(), "French; empty when clean."),
    }),
    # Lot 2A (T10) — explicit `required` lists: every key below is emitted
    # on every call (null where it does not apply), so a strict client can
    # rely on each.
    "create_template": _obj({
        "created": {"type": "boolean", "enum": [True]},
        "entity_type": {"type": "string", "enum": ["template"]},
        "entity": _template_summary(),
        "source_document_id": _str(
            "The stored document the file was taken from — never modified."),
        "leak_scan": _template_leak_report(),
        "scrubbed_properties": _scrubbed_properties(),
        # Lot 2B — always emitted: null without `substitutions`.
        "templatized": {
            **_obj({
                "substitution_count": _int(),
                "substituted_total": _int(),
                "substitutions": _arr(_obj({
                    "index": _int(
                        "Its position in your list, from 0 — the French "
                        "messages number from 1 (« n° 1 » is index 0)."),
                    "placeholder": _str("The field name inserted."),
                    "classification": _str("auto | manual | passthrough."),
                    "substituted": _int(
                        "Equal to your expected_occurrences."),
                    "in_alternate_branches": _int(
                        "Also replaced in a text box's second copy "
                        "(mc:Fallback), not counted above."),
                    "left_in_place": _int(
                        "Occurrences seen and NOT replaced (field results, "
                        "footnotes, document properties…)."),
                })),
                "rewritten_parts": _arr(_str("French label of a part.")),
            }),
            "type": ["object", "null"],
            "description": (
                "What the substitutions did; null when the file was "
                "registered unchanged (no `substitutions`)."),
        },
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }, required=["created", "entity_type", "entity", "source_document_id",
                 "leak_scan", "scrubbed_properties", "templatized",
                 "warnings", "idempotent_replay"]),
    "update_template": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "mode": {
            "type": "string", "enum": ["metadata", "file"],
            "description": (
                "metadata = name/description/category/kind; file = a new "
                "version of the file from source_document_id."),
        },
        "entity_type": {"type": "string", "enum": ["template"]},
        "entity": _template_summary(),
        "changed_fields": _arr(_str(
            "name | description | category | kind | file. [] = everything "
            "sent was already stored: nothing was written.")),
        "source_document_id": _nstr(
            "file: the stored document the new version was taken from — "
            "never modified. null on a metadata edit."),
        "file_replaced": _bool(
            "true = a NEW version of the file was installed by this call."),
        "replaced_version": _nint(
            "file: the version this call replaced — KEPT, restorable in the "
            "application; null when nothing was installed (a metadata edit, "
            "or a file identical to the version in force)."),
        "leak_scan": _template_leak_report(nullable=True),
        "scrubbed_properties": _scrubbed_properties(),
        # Fixups of lot 2A: a RENAME is checked against the dossiers the
        # template's files came from — when any is recorded.
        "name_check": {
            **_obj({
                "performed": _bool(
                    "false = no source dossier is recorded for this template "
                    "(or none still exists): the new name was checked "
                    "against NOTHING."),
                "dossier_ids": _arr(_str(), "The dossiers checked."),
                "missing_dossier_ids": _arr(
                    _str(), "Recorded source dossiers that no longer exist."),
                "accepted": _arr(_str(
                    "An identifier accepted in the name (accept_residual) — "
                    "it WILL print in every generated document's name.")),
                "unused_accept": _int(
                    "accept_residual entries matching nothing found."),
            }),
            "type": ["object", "null"],
            "description": (
                "The rename's identifier check; null when the name did not "
                "change (and on a file replacement)."),
        },
        "warnings": _arr(_str(), "French; empty when clean."),
        **_write_protocol_keys(),
    }, required=["updated", "mode", "entity_type", "entity",
                 "changed_fields", "source_document_id", "file_replaced",
                 "replaced_version", "leak_scan", "scrubbed_properties",
                 "name_check", "warnings", "idempotent_replay"]),

    # ── Lot 5b — ACCOUNTING (athena:comptabilite) ─────────────────────────
    "get_admin_ledger": _obj({
        "accounts": _arr(_admin_account_row(),
                         "Every administration account, even filtered."),
        "reconciliation_overdue": _bool("OR of the per-account flags."),
        "account_id": _nstr("The account the rows are of; null = every one."),
        "date_from": _str("YYYY-MM-DD — the window read."),
        "date_to": _str("YYYY-MM-DD."),
        "transactions": _arr(_admin_ledger_row(), "Newest first."),
        "count": _int(),
        "truncated": _bool(
            "true = more entries match than `limit`: narrow the window "
            "(there is no cursor)."),
        **_nmoney("opening_balance", (
            "The balance carried into date_from — null unless ONE account "
            "was read without a filter.")),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
    }),

    "record_trust_entry": _obj({
        "recorded": {"type": "boolean", "enum": [True]},
        "entity_type": {"type": "string",
                        "enum": ["trust_transaction", "trust_fee_payment"]},
        "entity": _trust_register_row(),
        "client_balance": _client_balance_block(),
        "admin_recette": _nullable(
            _admin_register_row(),
            "A fee payment's recette in the operations account; null "
            "otherwise."),
        "invoice": _invoice_payment_block(),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    }),

    "record_admin_entry": _obj({
        "recorded": {"type": "boolean", "enum": [True]},
        "entity_type": {"type": "string",
                        "enum": ["admin_transaction", "admin_card_payment"]},
        "entity": _admin_register_row(),
        "card_leg": _nullable(
            _admin_register_row(),
            "A card payment's card leg (the entity is its bank leg); null "
            "otherwise."),
        "invoice": _invoice_payment_block(),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    }),

    "update_admin_entry": _obj({
        "updated": {"type": "boolean", "enum": [True]},
        "outcome": {
            "type": "string", "enum": ["applied", "unchanged"],
            "description": (
                "« unchanged » = every value named was already stored: "
                "NOTHING was written (no etag moved)."),
        },
        "changed_fields": _arr(_str(), "The stored fields that changed."),
        "entity": _admin_register_row(),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    }),

    "clear_register_entries": _obj({
        "cleared": {"type": "boolean", "enum": [True]},
        "register": {"type": "string", "enum": ["trust", "admin"]},
        "account_id": _str(),
        "cleared_date": _str("YYYY-MM-DD — the statement date recorded."),
        "count": _int(),
        "entries": _arr(_obj({
            "id": _str(),
            "sequence": _int(),
            "date": _nstr("YYYY-MM-DD."),
            "direction": _str(),
            "status": _str("compensée."),
            "cleared_date": _nstr("YYYY-MM-DD."),
            **_money("amount"),
            "dossier_id": _str("'' when none."),
            "client_id": _str("trust only; '' otherwise."),
        })),
        "released_funds": _arr(_obj({
            "dossier_id": _str(),
            "client_id": _str(),
            **_money("amount"),
        }), "trust only: the cleared deposits each client may now draw on."),
        "warnings": _arr(_str(), "French; empty when nothing is amiss."),
        **_write_protocol_keys(),
    }),

    "reverse_register_entry": {
        "type": "object",
        "anyOf": [
            _reverse_branch("trust", _trust_register_row(), {
                "admin_reversals": _arr(_obj({
                    "admin_transaction_id": _str(),
                    "reversal_id": _str(),
                }), "A fee payment's administration recettes reversed with "
                    "it, in the same transaction."),
                **_nmoney("client_cleared_after", (
                    "The client's cleared balance after the reversal — "
                    "NEGATIVE is a trust shortfall to cover; null without a "
                    "client, and on a replay.")),
            }),
            _reverse_branch("admin", _admin_register_row(), {}),
        ],
    },
}
