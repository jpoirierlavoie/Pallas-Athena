"""What the connector says it can write, and what it says it never can.

The ONE place the write families and the « never » statements live (plan
rule 10). Two texts are DERIVED from it, and nothing else may restate them:

* ``INSTRUCTIONS`` (``mcp/endpoint.py``) — what the client model reads at
  ``initialize``, via :func:`build_instructions`;
* the consent screen (``templates/mcp/consent.html``) — the ONLY
  human-readable description of what a token can do, via
  :func:`consent_context`.

Before this registry the families were restated by hand in four places (the
two texts, the handlers' docstring, CLAUDE.md), and three of the restatements
had already drifted into false claims: the consent promised « chaque écriture
signée Claude » (only created notes, tasks and events carry a mention), both
texts said voiding an import « frees the number » (it stays attached to the
voided invoice), and two tool descriptions said an entry « can never be
edited » beside the tools that edit it.

**FAMILIES partition** :data:`mcp.tools.WRITE_TOOLS` — which is DERIVED from
them (:func:`write_tools`), so a write tool cannot ship without a family, and
the family is what puts it in the texts. Each family names its scope (checked
against every member's declared scope), its consent partial under
``templates/mcp/families/``, its clause of the consent checkbox summary, and
its INSTRUCTIONS paragraph (which must name every member literally).

**NEVERS** are the promises. Each one carries the CALLS it forbids — names
(full-match patterns over identifiers) and modules — which
``tests/test_mcp_disclosure.py`` sweeps over the connector's syntax tree
(calls, attributes, names, imports, ``getattr`` constants — never string
literals, so this file, which names them only as strings, cannot trip it).
A never that no call can express (a dossier's status is a PAYLOAD) is backed
by the input properties no write tool may declare and by a named behavioural
test that must exist. A later lot that falsifies a never DELETES it here —
the texts, the sweep and the consent follow — and appends its family.

Pure on purpose: it imports the scope constants and :mod:`markupsafe`,
nothing else — no model, no service, no ``mcp.tools`` at import time (which
derives ``WRITE_TOOLS`` from it). :func:`build_instructions` reads the tool
registry lazily, when the endpoint module is loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from markupsafe import Markup

from mcp import SCOPE_COMPTABILITE, SCOPE_WRITE


@dataclass(frozen=True)
class Family:
    """One write family: who may call it, and how it is described."""

    #: Stable identifier (lowercase), unique.
    key: str
    #: The heading INSTRUCTIONS use (« CREATE: … »).
    label: str
    #: The scope every member declares (``athena:write`` or
    #: ``athena:comptabilite``).
    scope: str
    #: The member tools, in the order the texts name them.
    tools: tuple[str, ...]
    #: The consent-screen partial, included inside the scope's block.
    consent_template: str
    #: This family's clause of the grant checkbox summary (static markup).
    checkbox_summary_fr: str
    #: The INSTRUCTIONS paragraph after « LABEL: ». May carry
    #: ``{phase_bulk_max}``, filled from the registry at build time.
    instructions_en: str


@dataclass(frozen=True)
class Never:
    """One promise, with the mechanism that keeps it true."""

    key: str
    #: The consent-screen bullet (static markup, no trailing punctuation).
    fr: str
    #: The INSTRUCTIONS sentence.
    en: str
    #: Identifier patterns (``re.fullmatch``) no connector module may
    #: reference — as a call, an attribute, a name, an import or a
    #: ``getattr`` constant.
    forbidden: tuple[str, ...] = ()
    #: Modules no connector module may import.
    forbidden_modules: tuple[str, ...] = ()
    #: Identifier patterns forbidden in the TOOL code only (the handlers),
    #: where the protocol layer has a legitimate use elsewhere — the OAuth
    #: store and the idempotency claim ``delete()`` bookkeeping documents.
    forbidden_in_tool_code: tuple[str, ...] = ()
    #: ``(call, keyword)``: every call to *call* in the connector must pass
    #: *keyword* — the shape of a promise about HOW a function is called.
    required_keywords: tuple[tuple[str, str], ...] = ()
    #: ``(tool, property)``: the input property the tool may not declare
    #: (``*`` = any write tool) — for a promise that is a payload.
    forbidden_inputs: tuple[tuple[str, str], ...] = ()
    #: ``path::test_name`` of the behavioural test backing a promise no
    #: sweep can express. The disclosure test checks that it exists.
    behavioural_test: str = ""
    #: The bullet to show INSTEAD while the accounting box is on the same
    #: page — the screen never forbids, two blocks up, what it offers two
    #: blocks down.
    fr_comptabilite: str = ""
    #: A short « jamais … » clause for the checkbox summary, when this
    #: promise belongs there.
    summary_fr: str = ""


# ── The write families ──────────────────────────────────────────────────

FAMILIES: tuple[Family, ...] = (
    Family(
        key="create",
        label="CREATE",
        scope=SCOPE_WRITE,
        tools=(
            "create_note", "append_to_note", "create_task", "create_hearing",
            "create_time_entry", "create_expense", "create_partie",
            "create_dossier", "complete_dossier", "record_signification",
            "record_prescription_event",
        ),
        consent_template="mcp/families/_create.html",
        checkbox_summary_fr=(
            "créer des notes, tâches, événements, temps, déboursés, "
            "contacts et dossiers, et compléter un dossier (champs encore "
            "vides, significations, événements de prescription)"
        ),
        instructions_en=(
            "notes (`create_note`, `append_to_note`), tasks "
            "(`create_task`), calendar events (`create_hearing`), billable "
            "time (`create_time_entry`), expenses (`create_expense`), "
            "contacts (`create_partie`), dossiers (`create_dossier`), plus "
            "three dossier recorders — `complete_dossier` fills ONLY fields "
            "that are still empty and refuses to overwrite anything, "
            "`record_signification` and `record_prescription_event` append "
            "to the dossier's registers."
        ),
    ),
    Family(
        key="correct",
        label="CORRECT",
        scope=SCOPE_WRITE,
        tools=(
            "update_partie", "update_dossier", "update_time_entry",
            "update_expense", "complete_task",
        ),
        consent_template="mcp/families/_correct.html",
        checkbox_summary_fr=(
            "corriger un contact, un dossier, une entrée de temps ou un "
            "déboursé non facturés&nbsp;; clore une tâche"
        ),
        instructions_en=(
            "these REPLACE the values you name, and a field you omit is "
            "left alone: `update_partie`, `update_dossier`, "
            "`update_time_entry` and `update_expense` (the last two only "
            "while the entry is not yet invoiced). `complete_task` closes a "
            "task (terminée, annulée) or puts an open one en_cours — it "
            "never reopens a closed task — and it is the only STATUS change "
            "here. One indirect effect to know: completing a task that a "
            "protocol step is linked to also completes that step, and if it "
            "was the last open one the whole protocol closes."
        ),
    ),
    Family(
        key="reclassify",
        label="RECLASSIFY",
        scope=SCOPE_WRITE,
        tools=(
            "set_time_entry_phase", "set_expense_phase",
            "set_time_entry_phase_bulk", "set_expense_phase_bulk",
        ),
        consent_template="mcp/families/_reclassify.html",
        checkbox_summary_fr=(
            "reclasser la phase du litige d'une entrée de temps ou d'un "
            "déboursé, même facturé (la phase ne paraît sur aucune facture)"
        ),
        instructions_en=(
            "`set_time_entry_phase` and `set_expense_phase` (plus "
            "`set_time_entry_phase_bulk` and `set_expense_phase_bulk`, up to "
            "{phase_bulk_max} rows a call) change ONLY the litigation phase, "
            "and are the only tools that reach a row already carried to an "
            "invoice. That is safe because the phase is on no invoice — it "
            "feeds the dossier budget's actuals — and they cannot touch "
            "hours, rate, amount or description. Use them to classify "
            "historical work; use `update_time_entry` for anything else, "
            "and only while the entry is unbilled."
        ),
    ),
    Family(
        key="import",
        label="IMPORT",
        scope=SCOPE_WRITE,
        tools=("import_invoice",),
        consent_template="mcp/families/_import.html",
        checkbox_summary_fr=(
            "reprendre des factures de votre ancien système (brouillon, "
            "numéro d'origine)"
        ),
        instructions_en=(
            "`import_invoice` recreates an invoice the practice's previous "
            "system already issued, under its own number and date. It NEVER "
            "allocates a number — the year counter is untouched — its line "
            "items can only come from real uninvoiced time entries and "
            "disbursements of that dossier, and the invoice lands in "
            "brouillon. Billing an entry freezes everything about it EXCEPT "
            "its litigation phase. To undo an import: void the invoice IN "
            "THE APPLICATION — that releases every time entry and "
            "disbursement it billed; the number stays on the voided invoice "
            "until the lawyer deletes that invoice in the application."
        ),
    ),
    Family(
        key="analyse",
        label="ANALYSE",
        scope=SCOPE_WRITE,
        tools=("record_document_analysis",),
        consent_template="mcp/families/_analyse.html",
        checkbox_summary_fr=(
            "analyser un document (nature, privilèges, niveau de protection "
            "— la catégorie est remplacée, le niveau ne descend jamais)"
        ),
        instructions_en=(
            "`record_document_analysis` records a document's qualification. "
            "You supply a `sous_nature` from the closed table "
            "(`get_reference_vocabulary`) and the `privileges` you identify; "
            "the CODE derives the nature, the family, the protection level "
            "and the document's category — never you. A level can only ever "
            "RISE: a re-analysis retaining fewer privileges keeps the stored "
            "level and flags the divergence, because under-protecting "
            "privileged material is a professional fault while "
            "over-protecting merely costs time. Only the lawyer, in the "
            "application, can lower one or confirm a qualification."
        ),
    ),
)


# ── The promises ────────────────────────────────────────────────────────

# The connector modules a « never » sweep reads: every ``mcp/*.py`` but this
# registry (which names the forbidden calls, as strings, in order to forbid
# them). The TOOL code — where a handler could reach a model — is the subset
# :attr:`Never.forbidden_in_tool_code` applies to; the protocol layer
# (OAuth store, idempotency claims) legitimately deletes its own bookkeeping.
SWEEP_EXCLUDED: frozenset[str] = frozenset({"mcp/disclosure.py"})
TOOL_CODE_MODULES: frozenset[str] = frozenset({
    "mcp/handlers.py", "mcp/coverage.py", "mcp/import_audit.py",
})

# Identifiers of every trust and administration register writer. Named here
# rather than by module: the connector READS trust (get_trust_* tools), so
# importing models.trust is legitimate — calling a writer is not.
_REGISTER_WRITERS: tuple[str, ...] = (
    "create_transaction", "update_transaction", "clear_transaction",
    "clear_transactions_bulk", "reverse_transaction",
    "create_inter_dossier_transfer", "create_card_payment",
    "attach_receipt", "create_reconciliation", "complete_reconciliation",
    "create_account", "update_account",
)

NEVERS: tuple[Never, ...] = (
    Never(
        key="delete",
        fr="<strong>supprimer</strong> quoi que ce soit",
        en="NOTHING can EVER be DELETED here.",
        forbidden=(r"delete_\w+", r"record_deletion"),
        # The protocol layer deletes its own bookkeeping (an expired OAuth
        # client, a released idempotency claim) — never a user record. The
        # tool code may not call a bare `.delete()` at all.
        forbidden_in_tool_code=("delete",),
        summary_fr="de suppression",
    ),
    Never(
        key="payment",
        fr="inscrire ou encaisser un <strong>paiement</strong>",
        fr_comptabilite=(
            "inscrire ou encaisser un <strong>paiement</strong> — sauf par "
            "une écriture aux registres comptables, avec la case "
            "«&nbsp;Autoriser la comptabilité&nbsp;»"
        ),
        en="This connector never records a payment.",
        forbidden=("record_payment", "projeter_paiement", "reduire_paiement"),
        forbidden_modules=("services.encaissements",),
        summary_fr="de paiement",
    ),
    Never(
        key="invoice_status",
        fr="envoyer une facture, changer son statut ou l'annuler",
        en=(
            "It never sends an invoice and never changes an invoice's "
            "status — voiding included."
        ),
        # void_invoice_report is void_invoice's own body since 2026-09-26
        # (void_invoice is its thin wrapper): the sweep matches names
        # exactly, so both must be named.
        forbidden=("update_status", "void_invoice", "void_invoice_report"),
        # Nothing outbound at all: the connector sends no email.
        forbidden_modules=("utils.courriel",),
    ),
    Never(
        key="invoice_number",
        fr=(
            "<strong>émettre un nouveau numéro de facture</strong>&nbsp;: "
            "le compteur annuel de l'application n'est jamais touché"
        ),
        en=(
            "It never allocates an invoice number: the application's year "
            "counter is never read or advanced."
        ),
        # The allocation moved INSIDE create_invoice's transaction
        # (2026-09-26): the standalone _generate_invoice_number is gone, and
        # these are the helpers that read or seed the year counter now.
        forbidden=(
            "_invoice_counter_ref", "_counter_seed", "_next_invoice_number",
            "_scan_max_invoice_seq",
        ),
        # create_invoice allocates from the counter whenever no number is
        # given — so every connector call must give one.
        required_keywords=(("create_invoice", "invoice_number"),),
        summary_fr="un nouveau numéro de facture",
    ),
    Never(
        key="dossier_status",
        fr=(
            "changer le statut d'un dossier — fermer un dossier doit vider "
            "sa collection DavX5, ce que seule l'application fait"
        ),
        en=(
            "A dossier's status is set at CREATION and can never be changed "
            "here: closing one requires a DavX5 drain only the application "
            "performs."
        ),
        forbidden_inputs=(
            ("update_dossier", "status"), ("complete_dossier", "status"),
        ),
        behavioural_test=(
            "tests/test_mcp_import.py::"
            "test_update_dossier_refuse_un_changement_de_statut_en_donnant_la_raison"
        ),
    ),
    Never(
        key="trust_identity",
        fr=(
            "toucher au <strong>fidéicommis</strong>, à la vérification "
            "d'identité ou à la vérification des conflits d'intérêts"
        ),
        fr_comptabilite=(
            "toucher au <strong>fidéicommis</strong> (sauf avec la case "
            "«&nbsp;Autoriser la comptabilité&nbsp;»), à la vérification "
            "d'identité ou à la vérification des conflits d'intérêts"
        ),
        en=(
            "It never touches trust accounting, identity verification or "
            "conflict-of-interest checks."
        ),
        forbidden=_REGISTER_WRITERS + (
            "update_kyc_status", "link_kyc_document",
        ),
        forbidden_modules=("models.admin_ledger",),
        forbidden_inputs=tuple(
            ("*", field) for field in (
                "identity_verified", "identity_verified_date",
                "identity_verified_notes", "conflict_check",
                "conflict_check_date", "conflict_check_notes",
                "kyc_document_ids",
            )
        ),
    ),
    Never(
        key="reopen_task",
        fr=(
            "<strong>rouvrir une tâche</strong> terminée ou annulée — cela "
            "se fait dans l'application"
        ),
        en=(
            "It never reopens a closed task (terminée or annulée): "
            "reopening a task is done in the application."
        ),
        # A four-state toggle: it sends annulée AND terminée back to
        # à_faire, silently un-cancelling a cancelled task.
        forbidden=("toggle_task_complete",),
        behavioural_test=(
            "tests/test_mcp_tools.py::"
            "test_a_closed_task_is_never_put_back_en_cours"
        ),
    ),
    Never(
        key="document",
        fr=(
            "modifier le <strong>fichier</strong> d'un document, son nom ou "
            "son dossier de classement — seule son analyse s'y inscrit, "
            "et la lecture de son contenu relève de l'accès en lecture "
            "ci-dessus"
        ),
        en=(
            "On a document the ONE thing you can write is its analysis; "
            "its file, its name and its folder are read-only here."
        ),
        forbidden=(
            "update_metadata", "move_document", "move_documents_bulk",
            "upload_document", "ingest_blob_as_document", "update_analyse",
            "confirmer_analyse", "create_folder", "rename_folder",
            "move_folder", "get_or_create_folder",
        ),
    ),
)


# ── Derivations ─────────────────────────────────────────────────────────


def write_tools() -> frozenset[str]:
    """Every write tool — the union of the families (``mcp.tools.WRITE_TOOLS``)."""
    return frozenset(name for family in FAMILIES for name in family.tools)


def families_for(scope: str) -> tuple[Family, ...]:
    """The families under *scope* that have at least one tool, in order."""
    return tuple(f for f in FAMILIES if f.scope == scope and f.tools)


def _never_fr(never: Never, comptabilite_offered: bool) -> Markup:
    text = never.fr_comptabilite if (comptabilite_offered and never.fr_comptabilite) else never.fr
    # Static constants of this module, never request data: safe to mark.
    return Markup(text)  # nosec B704 — module constant, no user input


def write_summary_fr() -> Markup:
    """The write checkbox's summary: every write family's clause, then the
    short « jamais » clauses — derived, so it names exactly what is granted."""
    clauses = [f.checkbox_summary_fr for f in families_for(SCOPE_WRITE)]
    body = "&nbsp;; ".join(clauses)
    body = body[:1].upper() + body[1:] + "."
    nevers = [n.summary_fr for n in NEVERS if n.summary_fr]
    if nevers:
        body += " Jamais " + ", jamais ".join(nevers) + "."
    return Markup(body)  # nosec B704 — module constants, no user input


def consent_context(*, comptabilite_offered: bool) -> dict:
    """What ``templates/mcp/consent.html`` renders from the registry.

    ``write_families`` are included, in order, inside the write block;
    ``nevers`` are the bullets of its « jamais » list — with the accounting
    variant of a bullet while the accounting box is on the same page;
    ``write_summary`` is the grant checkbox's summary; ``phase_bulk_max``
    is the reclassifiers' batch ceiling, read from the registry.
    """
    from mcp import tools as _tools  # lazy: mcp.tools imports this module

    return {
        "phase_bulk_max": _tools.PHASE_BULK_MAX,
        "write_families": families_for(SCOPE_WRITE),
        "comptabilite_families": families_for(SCOPE_COMPTABILITE),
        "nevers": [_never_fr(n, comptabilite_offered) for n in NEVERS],
        "write_summary": write_summary_fr(),
    }


# The paragraph about the ONE content-reading tool — a read, so no family,
# but the privilege warning belongs in the text every client model reads.
_READ_CONTENT_EN = (
    "READ-CONTENT: `get_document_text` reads a stored document's TEXT LAYER "
    "(PDF and .docx; take ids from list_documents; bounded per call — follow "
    "next_page). A scanned page has no text layer and is reported honestly "
    "(pages_without_text) — empty never means blank on paper, and nothing is "
    "OCR'd. Document content is privileged: quote only what the task "
    "requires."
)

_FORMATS_EN = (
    "Domain data (titles, notes, statuses, categories) is in French; note "
    "content is Markdown in French, raw HTML refused. Monetary amounts "
    "appear as integer `*_cents` plus a formatted `*_display` string (CAD). "
    "Datetimes are ISO 8601 in America/Montreal; date-only fields are "
    "`YYYY-MM-DD`. IDs are UUIDv4 strings — pass them between tools "
    "verbatim. Start broad (get_agenda, list_dossiers, search) and narrow "
    "with get_dossier / get_note / list_* filters."
)


def _backticked(names) -> str:
    names = [f"`{n}`" for n in names]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def build_instructions(
    registry: Optional[dict] = None,
    accounting_tools: Optional[frozenset] = None,
    phase_bulk_max: Optional[int] = None,
) -> str:
    """The ``initialize`` instructions, assembled from the registry.

    The counts, the family list and the list of tools accepting
    ``expected_etag`` are DERIVED — recopied by hand the counts went stale
    twice, and a model told « 29 read » looks for tools that are not there.
    The arguments exist for tests; by default the live registry is read
    (lazily: ``mcp.tools`` imports this module).
    """
    if registry is None or accounting_tools is None or phase_bulk_max is None:
        from mcp import tools as _tools

        registry = _tools.TOOLS if registry is None else registry
        accounting_tools = (
            _tools.ACCOUNTING_TOOLS if accounting_tools is None else accounting_tools
        )
        phase_bulk_max = (
            _tools.PHASE_BULK_MAX if phase_bulk_max is None else phase_bulk_max
        )

    writes = write_tools()
    families = [f for f in FAMILIES if f.tools]
    reads = len([n for n in registry if n not in writes])
    parts = [
        "Pallas Athena is a single-user Quebec civil litigation practice "
        f"manager. {reads} tools read; {len(writes)} write, in "
        f"{len(families)} families ("
        + ", ".join(f.label for f in families) + ").",
        _READ_CONTENT_EN,
    ]
    for family in families:
        parts.append(
            f"{family.label}: "
            + family.instructions_en.format(phase_bulk_max=phase_bulk_max)
        )

    scope = (
        "Write tools appear only when the lawyer granted the `athena:write` "
        "scope."
    )
    if accounting_tools:
        scope += (
            " Accounting tools appear only under the SEPARATE "
            "`athena:comptabilite` grant; `athena:write` never stands in "
            "for it."
        )
    parts.append(scope)
    parts.extend(n.en for n in NEVERS)

    etagged = sorted(
        name for name, spec in registry.items()
        if spec.get("concurrency") in ("optional", "required")
    )
    parts.append(
        "A write is permanent and may sync to the lawyer's phone — read the "
        "dossier before writing to it, and confirm with the user unless a "
        "standing instruction (a scheduled job, for example) already "
        "authorizes the write."
    )
    if etagged:
        parts.append(
            f"{_backticked(etagged)} accept `expected_etag`: pass the `etag` "
            "of the record from your latest read (each tool's "
            "`expected_etag` description names the reads that carry it) or "
            "from its last write result. If the record changed since — "
            "in the application, on the phone or through another call — the "
            "write is REFUSED and nothing is written: re-read, then retry. "
            "Omitted, the tool still refuses a change landing between its "
            "own read and its commit."
        )
    # The writes that take no etag but rewrite what they READ (a note plus
    # the appended block, a task's status and description, a dossier's
    # registers) compare-and-set against their own read too (critique,
    # lot 0a): a caller seeing their `stale_etag` refusal must know it is
    # a race, and that the remedy is a re-read — not a changed argument.
    parts.append(
        "The writes that rewrite what they read — appending to a note, "
        "closing a task, filling or appending to a dossier — refuse the "
        "same way when the record changed during the call: nothing is "
        "written; re-read, then send the call again."
    )
    parts.append(
        "Every write tool accepts `idempotency_key` (any stable string you "
        "choose; retrying with the SAME key within 24 h returns the "
        "original result instead of duplicating). Always pass an "
        "idempotency_key; if a write without one appeared to fail, re-read "
        "(list/get) before retrying. A refusal saying a call with that key "
        "is still in flight means: wait, then retry with the SAME key — "
        "never a new one."
    )
    parts.append(
        "A result reading « ENREGISTRÉE — NE PAS RÉESSAYER » means the write "
        "COMMITTED and a later step failed: do NOT retry — re-read the "
        "record; a call without the same key would write it twice."
    )
    parts.append(
        "Provenance: every record this connector writes directly is stamped "
        "`updated_via` \"mcp\" (and `created_via` \"mcp\" when it created "
        "it), with the time of its last connector write in "
        "`mcp_updated_at` — returned by the read rows whose output schema "
        "declares those keys, which not every read does; the notes, tasks "
        "and events it CREATES also carry a dated « … par Claude le … » "
        "line."
    )
    parts.append(_FORMATS_EN)
    return " ".join(parts)
