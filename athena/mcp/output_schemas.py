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
) -> dict:
    """An object schema. ``required=None`` requires EVERY listed key —
    the common case, since handlers build their dicts unconditionally."""
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    keys = list(properties) if required is None else required
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
    so ONE bare-anyOf descriptor would fail all 19 tools at once). Draft
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

def _dossier_write_result(verb: str) -> dict:
    """The success payload of a dossier write.

    NO ctag_bumped / dav_synced: dossiers are not a DAV collection, and this
    module's rule is never to declare a sync key a write cannot honour.
    """
    return _obj({
        verb: {"type": "boolean", "enum": [True]},
        "entity_type": _str("Always « dossier »."),
        "entity": _obj({
            "id": _str(),
            "dossier_id": _str("Same value as id — the audit log reads this."),
            "file_number": _str(),
            "label": _str("The dossier title."),
            "status": _str(),
            "legacy_ref": _str("'' when not imported."),
        }),
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
    })


def _partie_write_result(verb: str) -> dict:
    """The success payload of a contact write.

    Carries ctag_bumped/dav_synced because parties ARE DAV-exposed (CardDAV,
    /dav/addressbook/) — unlike time entries and disbursements, whose result
    deliberately declares neither rather than fake a sync that does not
    exist.
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
        }),
        "ctag_bumped": _bool(
            "The addressbook CTag moved, so DavX5 will re-sync. false with a "
            "warning means the write landed but the sync was not triggered — "
            "do NOT retry, it would duplicate the contact."
        ),
        "dav_synced": _bool("The contact will reach the phone."),
        "warnings": _arr(_str("French; empty when nothing is amiss.")),
        **_write_protocol_keys(),
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


def _stamps() -> dict[str, Any]:
    """The created_at/updated_at pair every row carries (PA-G05) — true
    instants (iso_mtl), nullable on pre-Rule-7 legacy docs. updated_at is
    NOISY: DAV round-trips, protocol-step syncs and bulk folder moves all
    re-stamp it without visible content changing."""
    return {
        "created_at": _nstr("ISO-8601 Montréal."),
        "updated_at": _nstr(
            "ISO-8601 Montréal. Noisy: phone syncs and internal "
            "bookkeeping re-stamp it without visible changes."),
    }


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
        **_stamps(),
    })


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
    return _obj(properties)


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
        "date_confirmed": _bool(),
        "completed_date": _nstr(),
        "linked_task_id": _nstr(),
        "linked_hearing_id": _nstr(),
        "notes": _str(),
        "is_overdue": _bool(
            "Equivalent to `status == \"en_retard\"` — both come from one "
            "predicate, so they can never contradict each other."
        ),
        **_stamps(),
    }
    if extra:
        properties.update(extra)
    return _obj(properties)


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
    })


def _invoice_row(extra: Optional[dict[str, Any]] = None) -> dict:
    """SHARED by list_invoices, get_invoice and
    get_billing_snapshot.outstanding_invoices — one row shape, no drift."""
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
    return _obj(properties)


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
    })


def _draft_summary(id_note: str = "") -> dict:
    """The draft snapshot both write tools and get_draft emit (Phase N).

    NO ctag/dav keys anywhere in the draft family: chat_drafts is not a DAV
    collection, and this module's rule is never to declare a sync key a
    write cannot honour.
    """
    return _obj({
        "id": _str(id_note or "Draft UUIDv4."),
        "dossier_id": _str("Empty string for a floating draft. PERMANENT — "
                           "a draft never moves between dossiers."),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "title": _str(),
        "current_version": _int("Versions are 1..current_version, all "
                                "stored, none deletable."),
        "content_length": _int("Stored length AFTER sanitization — compare "
                               "against what was sent to detect any loss."),
        "created_at": _nstr(),
        "updated_at": _nstr(),
    })


def _draft_write_result(verb: str) -> dict:
    """save_draft / revise_draft success payload (Phase N)."""
    return _obj({
        verb: {"type": "boolean", "enum": [True]},
        "draft": _draft_summary(
            "Empty when nothing was written."
        ),
        "warnings": _arr(_str(), "French, human-readable; empty when clean."),
        **_write_protocol_keys(),
    })


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


def _written_entity(extra: dict[str, Any]) -> dict:
    """The WP16 creators' entity snapshot — common core + per-tool keys."""
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
    return _obj(props)


def _entity_write_result(
    entity_extra: dict[str, Any], *, dav: bool, verb: str = "created",
    extra: Optional[dict[str, Any]] = None,
) -> dict:
    """Result contract of a WP16 creator / WP17 recorder.

    DAV-exposed entities (task, hearing) carry ctag_bumped/dav_synced;
    time entries, expenses and dossier-array additions are not DAV-exposed
    and deliberately do NOT declare those keys — faking them would claim a
    sync that does not exist."""
    props: dict[str, Any] = {
        verb: {"type": "boolean", "enum": [True]},
        "entity_type": _str(),
        "entity": _written_entity(entity_extra),
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
    if extra:
        props.update(extra)
    return _obj(props)


def _record_prescription_event_result() -> dict:
    """record_prescription_event's contract: the recorder result PLUS the
    answer that motivated the call — what the delay looks like NOW."""
    base = _entity_write_result({
        "type": _str(),
        "reference": _str(),
    }, dav=False, verb="recorded")
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
                "created_at": _nstr(),
                "updated_at": _nstr(),
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
            }),
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
            "window": _obj({"from": _str(), "to": _str()}),
            **_next_cursor("Hearings page oldest-first, so it advances "
                           "forward in time."),
        },
        extra_required=["window", "next_cursor"],
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
            "(the Analyse sheet) — readable here but READ-ONLY: "
            "append_to_note refuses it."
        ),
        "created_at": _nstr(),
        "updated_at": _nstr(),
        "content_preview": _str("First 280 characters, plain text."),
    }), extra={
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
                    "note (the Analyse sheet) — readable but READ-ONLY: "
                    "append_to_note refuses it."
                ),
                "created_at": _nstr(),
                "updated_at": _nstr(),
            }),
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
            "true = `category` vient d'une analyse et non d'une "
            "détermination de l'avocat."),
            **_stamps(),
        }),
        # Present ONLY when the request carried folder_id — typed, never
        # required (kept for compatibility; the per-row folder_path is the
        # general resolver).
        extra={
            "folder_path": _str("Breadcrumb of the REQUESTED folder. "
                                "Only present when folder_id was given."),
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

    "list_parties": _list_envelope(_obj({
        "id": _str(),
        "display_name": _str(),
        "type": _str(),
        "contact_role": _str(),
        "is_organization": _bool(),
        "city": _str(),
        **_stamps(),
    }), extra=_next_offset()),

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
                "kyc_document_ids": _arr(_str()),
                "mandataires": _arr(
                    _obj({"id": _str(), "kind": _str(), "notes": _str()},
                         required=[]),
                    "Model-owned entries {id, kind, notes}.",
                ),
                "notes": _str(),
                "created_at": _nstr(),
                "updated_at": _nstr(),
            }),
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
        "created_via": _str(
            "« mcp » when this connector recorded it, '' otherwise."
        ),
        **_phase_pair(),
        **_stamps(),
        **_money("rate"),
        **_money("amount"),
    }), extra=_next_cursor(), extra_required=["next_cursor"]),

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
        "created_via": _str(
            "« mcp » when this connector recorded it, '' otherwise."
        ),
        **_phase_pair(),
        **_stamps(),
        **_money("amount"),
    }), extra=_next_cursor(), extra_required=["next_cursor"]),

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
            }),
        }),
        {"invoice_id": _str("Echo of the id that was not found.")},
    ),

    "create_partie": _partie_write_result("created"),
    "update_partie": _partie_write_result("updated"),
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
        }),
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
        }),
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
        }),
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
        }),
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
            "status": _str("Always « brouillon » — never promoted here."),
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

    "create_dossier": _dossier_write_result("created"),
    "update_dossier": _dossier_write_result("updated"),

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
                    "What to do IN THE APPLICATION — the connector cannot "
                    "delete, void, or change an invoice's status."
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
                    "French. Says what to do IN THE APPLICATION: this "
                    "connector cannot create a protocol, verify an identity "
                    "or file a signification."
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
        "entity_type": _str(),
        "entity_id": _str(),
        "dossier_id": _str("Empty when the entity had no dossier."),
        "title": _str("Minimal snapshot — never the deleted content."),
        "status": _str("The entity's status/category at deletion."),
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
            "steps": _arr(_step_row()),
            **_stamps(),
        })),
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

    "create_task": _entity_write_result({
        "status": _str("Always « à_faire » — a created task is WORK, "
                       "never history."),
        "priority": _str(),
        "category": _str(),
        "phase": _str("Phase du litige (code, '' = non renseignée)."),
        "sous_phase": _str("Sous-code de phase ('' = non renseignée)."),
    }, dav=True),

    "create_hearing": _entity_write_result({
        "hearing_type": _str(),
        "forum": _str("Derived from the type."),
        "all_day": _bool(),
    }, dav=True),

    "create_time_entry": _entity_write_result({
        "hours": _num(),
        "billable": _bool(),
        "phase": _str("Phase du litige (code, '' = non renseignée)."),
        "sous_phase": _str("Sous-code de phase ('' = non renseignée)."),
        **_money("rate"),
        **_money("amount"),
    }, dav=False),

    "create_expense": _entity_write_result({
        "category": _str(),
        "taxable": _bool(),
        "phase": _str("Phase du litige (code, '' = non renseignée)."),
        "sous_phase": _str("Sous-code de phase ('' = non renseignée)."),
        **_money("amount"),
    }, dav=False),

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
    }),

    "record_signification": _entity_write_result({
        "partie_id": _str(),
        "mode": _str(),
        "confirmee": _bool(),
    }, dav=False, verb="recorded"),

    "record_prescription_event": _record_prescription_event_result(),

    # ── Phase N — document content + versioned drafts ───────────────────

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

    "get_draft": _found_or_not(
        _obj({
            "found": _found(True),
            "draft": _draft_summary(),
            "version_shown": _int(
                "Which version `content` carries — current_version unless "
                "a specific one was requested."),
            "content": _str("The FULL Markdown text of that version."),
        }),
        {"draft_id": _str()},
    ),

    "list_drafts": _list_envelope(_draft_summary()),

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
        # `category` et `category_source` manquent en branche SÈCHE —
        # rien n'est écrit, donc rien n'est stocké à rapporter. `required`
        # ne porte donc que ce qui est TOUJOURS présent.
        "required": ["recorded", "document_id", "analyse", "warnings"],
    },
    "save_draft": _draft_write_result("created"),

    "revise_draft": _draft_write_result("revised"),
}
