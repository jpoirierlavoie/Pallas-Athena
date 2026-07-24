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


def _money(key: str) -> dict[str, Any]:
    """The §10.1 money pair, to splat into a properties dict."""
    return {
        f"{key}_cents": {"type": "integer"},
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
        "completed_date": _nstr(),
        "dossier_id": _nstr("null for a « Général » (standalone) task."),
        "dossier_file_number": _str(),
        "dossier_title": _str(),
        "related_note_id": _nstr("Linked parent note (RFC 5545 RELATED-TO)."),
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
        "status": _str(),
        "mandatory": _bool(),
        "deadline_locked": _bool(),
        "date_confirmed": _bool(),
        "completed_date": _nstr(),
        "linked_task_id": _nstr(),
        "linked_hearing_id": _nstr(),
        "notes": _str(),
        "is_overdue": _bool("Derived by date comparison — a step due today is not overdue."),
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
        "prescription_date": _nstr("The computed « date pour agir », YYYY-MM-DD."),
        "clients": _arr(_str(), "Client NAMES (strings) in this summary row."),
        "opposing_parties": _arr(_str()),
    })


def _invoice_row() -> dict:
    return _obj({
        "id": _str(),
        "invoice_number": _str(),
        "dossier_id": _str(),
        "dossier_file_number": _str(),
        "client_name": _str(),
        "date": _nstr("YYYY-MM-DD."),
        "due_date": _nstr("YYYY-MM-DD."),
        "status": _str(),
        **_money("total"),
        **_money("amount_due"),
    })


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


def _write_result(verb: str, extra: Optional[dict[str, Any]] = None) -> dict:
    properties: dict[str, Any] = {
        verb: {"type": "boolean", "enum": [True]},
        "note": _written_note(),
        "ctag_bumped": _bool("Whether the DavX5 sync trigger fired. false = "
                             "the write COMMITTED but the phone will only "
                             "catch up on the next change; do not retry."),
        "dav_synced": _bool("ctag_bumped AND the collection is visible to "
                            "DavX5 (a fermé/archivé dossier's is not)."),
        "warnings": _arr(_str(), "French, human-readable; empty when clean."),
    }
    if extra:
        properties.update(extra)
    return _obj(properties)


# ── The registry ────────────────────────────────────────────────────────

OUTPUT_SCHEMAS: dict[str, dict] = {
    "get_agenda": _obj({
        "window": _obj({
            "from": _str("YYYY-MM-DD, Montréal."),
            "to": _str(),
            "days_ahead": _int(),
        }),
        "hearings": _arr(_hearing_row(), "Upcoming, annulée excluded here."),
        "urgent_tasks": _arr(_task_row({"is_overdue": _bool()})),
        "urgent_protocol_steps": _arr(_step_row({
            "protocol_id": _str(),
            "protocol_title": _str(),
            "dossier_file_number": _str(),
        })),
        "prescription_alerts": _arr(_obj({
            "dossier_id": _str(),
            "file_number": _str(),
            "title": _str(),
            "prescription_date": _nstr("YYYY-MM-DD."),
            "days_remaining": _nint(),
            "last_action_date": _nstr("Previous juridical day — the real "
                                      "last day to act."),
            "prescription_notes": _str(),
        })),
        "stats": _obj({
            "open_dossiers": _int(),
            "unbilled_hours": _num(),
            **_money("unbilled"),
            **_money("outstanding"),
        }),
    }),

    "list_dossiers": _list_envelope(_dossier_list_row()),

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
                "prescription_notes": _str(),
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

    "list_tasks": _list_envelope(_task_row()),

    "list_hearings": _list_envelope(
        _hearing_row(),
        extra={"window": _obj({"from": _str(), "to": _str()})},
        extra_required=["window"],
    ),

    "list_notes": _list_envelope(_obj({
        "id": _str(),
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
    })),

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
            "display_name": _str(),
            "category": _str(),
            "file_type": _str("MIME type."),
            "file_size": _int("Bytes."),
            "file_size_display": _str(),
            "version": _int(),
            "folder_id": _nstr("null = dossier root."),
            "description": _str(),
            "tags": _arr(_str()),
            "created_at": _nstr(),
        }),
        # Present ONLY when the request carried folder_id — typed, never
        # required.
        extra={"folder_path": _str("Breadcrumb, « Parent / Enfant ». Only "
                                   "present when folder_id was given.")},
    ),

    "list_parties": _list_envelope(_obj({
        "id": _str(),
        "display_name": _str(),
        "type": _str(),
        "contact_role": _str(),
        "is_organization": _bool(),
        "city": _str(),
    })),

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
            **_money("outstanding"),
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

    "list_protocol_steps": _obj({
        "dossier_id": _str(),
        "has_active_protocol": _bool(),
        "protocols": _arr(_obj({
            "id": _str(),
            "title": _str(),
            "protocol_type": _str(),
            "status": _str(),
            "court": _str(),
            "start_date": _nstr(),
            "end_date": _nstr(),
            "notes": _str(),
            "steps": _arr(_step_row()),
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
    }),

    "get_trust_snapshot": _obj({
        "accounts": _arr(_obj({
            "id": _str(),
            "name": _str(),
            "institution": _str(),
            "account_type": _str(),
            **_money("book_balance"),
            **_money("bank_balance"),
        }, description="Never includes the transit or account number.")),
        **_money("total_held"),
        "outstanding_count": _int(),
        "outstanding_total_cents": _int(),
        "in_transit_count": _int(),
        "in_transit_total_cents": _int(),
        "last_reconciliation_date": _nstr(),
        "reconciliation_overdue": _bool(),
    }),

    "create_note": _write_result("created"),

    "append_to_note": _write_result(
        "appended", {"appended_chars": _int(
            "Length of the appended block, separator and provenance "
            "stamp included.")},
    ),
}
