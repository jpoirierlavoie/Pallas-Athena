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
            "déboursé non facturés — ou les déplacer vers un autre "
            "dossier&nbsp;; clore une tâche"
        ),
        instructions_en=(
            "these REPLACE the values you name, and a field you omit is "
            "left alone: `update_partie`, `update_dossier`, "
            "`update_time_entry` and `update_expense` (the last two only "
            "while the entry is not yet invoiced; their `dossier_id` MOVES "
            "the entry to another dossier, its amount and phase kept — both "
            "dossiers' budget actuals change). `complete_task` closes a "
            "task (terminée, annulée) or puts an open one en_cours; it never "
            "reopens a closed one — that is `reopen_task` (AGENDA). One "
            "indirect effect to know: completing a task that a protocol step "
            "is linked to also completes that step, and if it was the last "
            "open one the whole protocol closes. The reverse, rare: putting "
            "an open task en_cours while its linked step is still complété "
            "reopens that step, and a protocol its last step had closed."
        ),
    ),
    Family(
        key="agenda",
        label="AGENDA",
        scope=SCOPE_WRITE,
        tools=(
            "update_task", "reopen_task", "update_note", "edit_analyse",
            "create_protocol", "update_protocol", "add_protocol_step",
            "update_protocol_step", "update_hearing", "create_hearing_series",
        ),
        consent_template="mcp/families/_agenda.html",
        checkbox_summary_fr=(
            "modifier ou déplacer une tâche ou une note, rouvrir une tâche, "
            "rédiger la théorie de la cause (le texte remplacé d'une note "
            "est conservé), créer et tenir le protocole de l'instance et "
            "ses étapes, modifier, reporter, annuler ou déplacer un "
            "événement du calendrier (d'un rendez-vous Bookings confirmé, "
            "seulement le dossier et les notes) et créer une série "
            "d'événements"
        ),
        instructions_en=(
            "`update_task` and `update_note` REPLACE the fields you name (a "
            "field you omit is left alone; values already stored write "
            "nothing); a `dossier_id` MOVES the item, and its phone copy "
            "follows. A task's replaced description is NOT kept; a note's "
            "replaced content IS kept in its revision history, whatever "
            "replaced it (the app, the phone, an append or an edit here), "
            "and `update_note` demands `expected_etag` to replace content. "
            "`reopen_task` reopens a closed task (an annulée one only with "
            "reopen_cancelled true) together with the protocol step linked "
            "to it and a protocol the cascade had closed — and refuses, "
            "writing nothing, when that step cannot follow. `edit_analyse` "
            "writes the dossier's « Théorie de la cause »: without "
            "operations it creates the note if needed and returns its "
            "structure and etag; with operations it replaces or completes "
            "whole blocs (entete, A to H), or rewrites the note keeping its "
            "eight headings — `expected_etag` required, each replaced "
            "version kept. Protocols: `create_protocol` creates a "
            "dossier's protocol from its template (one actif per dossier; "
            "no task unless create_linked_tasks); `update_protocol` replaces "
            "its title, notes, court, start_date or status — a new "
            "start_date recomputes the template deadlines except completed "
            "steps and truly-confirmed CS dates; `add_protocol_step` adds a "
            "custom step to an actif protocol; `update_protocol_step` "
            "replaces a step's deadline, notes or phase (a template step's "
            "C.p.c. text is locked) OR sets its status to a TARGET "
            "(complété, à_venir — never a toggle): the linked task follows, "
            "completing the last open step closes the whole protocol, and "
            "reopening a step of a protocol closed that way reactivates it. "
            "A protocol's or a step's replaced notes are NOT kept. "
            "Calendar: `update_hearing` REPLACES an event's fields — a "
            "reschedule keeps the Montréal hour and the duration you do "
            "not name; status annulée removes the event's Outlook copy; a "
            "series occurrence changes dossier only with "
            "detach_from_series; replaced event notes are NOT kept — and on "
            "a CONFIRMED Bookings rendez-vous only its dossier and its notes "
            "change: rescheduling, cancelling or any other edit is REFUSED, "
            "since the Outlook meeting the client holds is the reference "
            "(a reschedule or a cancellation is made in Outlook, anything "
            "else by the lawyer in the application). "
            "`create_hearing_series` writes a recurring series in one "
            "atomic batch (idempotency_key required). Never retry an edit "
            "blindly after a stale_etag refusal: re-read, then redo it on "
            "the current text."
        ),
    ),
    Family(
        key="bookings",
        label="BOOKINGS",
        scope=SCOPE_WRITE,
        tools=("decide_rendez_vous",),
        consent_template="mcp/families/_bookings.html",
        checkbox_summary_fr=(
            "confirmer ou refuser une demande de rendez-vous Bookings — "
            "refuser annule la réunion Outlook, ce qui prévient le client "
            "par un texte fixe"
        ),
        instructions_en=(
            "`decide_rendez_vous` decides a pending « Bookings with me » "
            "request, listed by `list_hearings` with bookings \"pending\": "
            "`confirmer` puts it in the calendar (linking the contact "
            "matched on the requester's exact email unless lier_partie is "
            "false); `refuser` CANCELS THE OUTLOOK MEETING AND SO NOTIFIES "
            "THE CLIENT, with a fixed text — the connector's ONLY effect "
            "that reaches anyone outside the practice. `expected_etag` and "
            "`idempotency_key` are required; a repeated decision writes "
            "nothing and contacts nobody. Confirm "
            "with the user every time. A rendez-vous once CONFIRMED is "
            "neither refused nor rescheduled nor cancelled here: that is "
            "done in Outlook."
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
            "and are the only tools that change a row WHILE it stays carried "
            "to an invoice (a void — BILL — releases the row instead). That "
            "is safe because the phase is on no invoice — it "
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
            "its litigation phase. To undo an import: void the invoice "
            "(`update_invoice`, status annulée — see BILL — or in the "
            "application); that releases every time entry and disbursement "
            "it billed, and the number stays on the voided invoice until the "
            "lawyer deletes that invoice in the application."
        ),
    ),
    Family(
        key="billing",
        label="BILL",
        scope=SCOPE_WRITE,
        tools=("create_invoice", "update_invoice", "create_budget_version"),
        consent_template="mcp/families/_billing.html",
        checkbox_summary_fr=(
            "émettre une nouvelle facture au brouillon (elle consomme "
            "définitivement le prochain numéro de l'année), corriger un "
            "brouillon, marquer une facture envoyée ou en retard — ce qui "
            "n'envoie rien —, annuler une facture qui ne porte aucun "
            "paiement, et enregistrer une nouvelle version du budget d'un "
            "dossier"
        ),
        instructions_en=(
            "`preview_invoice` (a read) shows what an invoice would be — the "
            "SAME computation as the write: run it first. `create_invoice` "
            "then issues it, always in brouillon, from real billable UNBILLED "
            "sources of one dossier, with the preview's total as "
            "expected_total_cents (any difference refuses) and an "
            "idempotency_key (required). It CONSUMES the year's next "
            "number (AAAA-F###) for ever: a void never gives it back. The "
            "sources it bills are frozen, their phase aside, until a void "
            "releases them. "
            "`update_invoice` makes ONE change a call against the invoice's "
            "etag (`get_invoice`): correct a brouillon — ONLY a brouillon; an "
            "issued invoice is corrected by voiding it and issuing a new one "
            "— its notes, payment terms, due date or billing address, never "
            "an amount, a line, its client or its number; set a status — "
            "brouillon → "
            "envoyée, envoyée ↔ en_retard (only past its due date) — which "
            "SENDS NOTHING to anyone, a promotion being undone only by a "
            "void; or void it (status annulée, with a void_reason), which "
            "releases every source it billed and is REFUSED while a payment "
            "stands. The Word note "
            "d'honoraires is `create_document` with source invoice_note "
            "(FILES). Budgets: `get_budget` (a read) gives the version in "
            "force and its base_version; `create_budget_version` records a "
            "NEW version — replace or merge — refused when a newer one was "
            "saved since; it becomes the reference budget, whose estimate "
            "is a client document, and every earlier version is kept, "
            "unchanged — the proof of what the client was told, and when."
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
            "the CODE derives from them the nature, the family, the "
            "protection level and the document's category — you never pick "
            "those here (a category you set in FILES is PRESUMED and only for "
            "a document WITHOUT an analysis; an analysis replaces it). A "
            "level can only ever "
            "RISE: a re-analysis retaining fewer privileges keeps the stored "
            "level and flags the divergence, because under-protecting "
            "privileged material is a professional fault while "
            "over-protecting merely costs time. Only the lawyer, in the "
            "application, can lower one or confirm a qualification."
        ),
    ),
    Family(
        key="files",
        label="FILES",
        scope=SCOPE_WRITE,
        tools=("update_document", "move_documents", "manage_folder",
               "fill_gabarit", "create_document",
               "begin_upload", "finalize_upload"),
        consent_template="mcp/families/_files.html",
        checkbox_summary_fr=(
            "classer un document (nom, date, étiquettes, dossier de "
            "classement, catégorie présumée), organiser les dossiers de "
            "classement (jamais les dossiers système), produire de "
            "nouveaux projets Word — depuis un gabarit, depuis un texte "
            "rédigé par Claude, par copie d'un .docx du même dossier, la "
            "note d'honoraires d'une facture —, "
            "téléverser un fichier par un lien de dépôt en écriture seule, "
            "comme nouveau document ou comme gabarit"
        ),
        instructions_en=(
            "`update_document` REPLACES a document's filing fields you name "
            "— display name, date, tags, folder — never its file and never "
            "notes_internes, the lawyer's own text. It may also set the "
            "category of a document that carries NO analysis: stored "
            "PRESUMED, shown « présumée » until the lawyer confirms it in the "
            "application (an analysed document's category is "
            "`record_document_analysis`'s) — never over a category the "
            "LAWYER chose or confirmed (`category_set_by_lawyer`: refused; "
            "tell him instead). `move_documents` refiles several "
            "documents of one dossier into one folder in one atomic write, "
            "answering each id. `manage_folder` creates, renames or moves a "
            "folder of the filing tree; the system folders « Projets » and "
            "« Reçus du portail » belong to the application and are never "
            "renamed, moved or recreated here (filing documents INTO them is "
            "allowed). Ids and etags come from `list_documents` "
            "(include_folders for the tree). New Word files are always NEW "
            "documents, drafts never sent: `fill_gabarit` fills a gabarit "
            "for a dossier, ALWAYS into its « Projets » — you write ONLY the "
            "blocs and manual fields `list_templates` (with template_id) "
            "reports, the application resolves every other field, and a "
            "text holding "
            # Doubled: this paragraph goes through str.format().
            "« {{{{ » or « }}}} » is refused; `create_document` prints your "
            "Markdown on the note-print template the lawyer designated "
            "ACTIVE (refused when none is), or copies a stored .docx within "
            "its OWN dossier — the copy keeping the source's protection "
            "level, presumed, and its category, presumed unless the lawyer "
            "had set it — into « Projets » unless you give "
            "folder_id. Reuse across dossiers goes through a gabarit, never "
            "a copy; with source invoice_note it files an invoice's Word "
            "note d'honoraires, on the note-d'honoraires template the lawyer "
            "designated active, into « Projets » — a note identical to what "
            "it would print is returned rather than filed twice. To bring an "
            "OUTSIDE file in, `begin_upload` opens a "
            "one-hour write-only ticket — its `upload_url`, the one link any "
            "result carries, is for a single PUT from your code sandbox, "
            "never to be shown to the user; the PUT needs the sandbox to "
            "reach storage.googleapis.com (a claude.ai organisation "
            "setting): if the network refuses it, say so and stop — the "
            "ticket expires unused, nothing filed — and `finalize_upload` "
            "files the bytes only if their size and MD5 are the ones you "
            "declared, as a NEW document or a template (see TEMPLATES). "
            "Refused or expired bytes are discarded: they were never filed."
        ),
    ),
    Family(
        key="templates",
        label="TEMPLATES",
        scope=SCOPE_WRITE,
        tools=("create_template", "update_template"),
        consent_template="mcp/families/_templates.html",
        checkbox_summary_fr=(
            "enregistrer comme gabarit un .docx déjà versé à un dossier, tel "
            "quel ou transformé en gabarit (ses textes propres au dossier "
            "remplacés par des champs), et corriger un gabarit (nom, "
            "description, catégorie, type, ou nouvelle version de son "
            "fichier, la précédente conservée)"
        ),
        # A brace is DOUBLED here: build_instructions runs str.format over
        # this text (for {phase_bulk_max}), which halves it — « {{{{field}}}} »
        # reads « {{field}} » in INSTRUCTIONS (pinned).
        instructions_en=(
            "`create_template` registers a .docx ALREADY in a dossier as a "
            "NEW template, its bytes unchanged (scrub_properties aside: it "
            "empties the file's document properties) — or, given "
            "`substitutions`, a TEMPLATIZED copy, each literal of the matter "
            "(a name, a number, an address) replaced by its {{{{field}}}}, "
            "its document properties always emptied: run "
            "`preview_templatize` (a read) FIRST — it counts every "
            "occurrence, part by part, says what stays in place and what "
            "would remain of the dossier — adjust, then pass each count as "
            "`expected_occurrences`; one count that differs refuses the "
            "whole call, and matching is case-SENSITIVE, so an ALL-CAPS "
            "variant needs its own substitution. `update_template` "
            "corrects a template's name, description, category or kind — or "
            "installs a stored .docx as a NEW version of its file (the one "
            "in force kept, restorable in the application). An OUTSIDE .docx "
            "becomes a template, or a NEW version of one, through "
            "`begin_upload` (purpose gabarit) and `finalize_upload` — never "
            "templatized there: to TEMPLATIZE one, file it first as a "
            "document of its dossier (purpose document), then preview and "
            "templatize that document. Ids, "
            "versions and etags come from `list_templates`. A file taken "
            "from a dossier document is ALWAYS checked against that "
            "document's own dossier (a templatized one as it will be stored, "
            "your literals included), an uploaded one against the dossier_id "
            "you MUST name — unless it comes from no dossier and you declare "
            "aucun_dossier_source: true, when nothing is checked — refused "
            "while it, or the name of a "
            "template it CREATES, still names that dossier's parties, "
            "numbers or addresses, unless the lawyer accepts each residue. "
            "A new name you give a template that exists is checked the same "
            "way against the dossiers its files came from, when the "
            "application recorded them (`name_check` says whether it was); "
            "it prints in the name of every document drawn from the "
            "template, for any client: never put a party's name in it. A "
            "special kind is never made "
            "active, and the active template's kind never changes here; a "
            "new file for the ACTIVE template prints at once on every "
            "document of its kind."
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
        # Lot 1b made cancelling a CAPABILITY (a task, an event, a Bookings
        # request): the promise is about Athéna's records, which a
        # cancellation keeps — the Outlook side of a cancelled event or of a
        # refused request is disclosed by its family, beside the capability.
        fr=(
            "<strong>supprimer</strong> quoi que ce soit dans Athéna — "
            "annuler une tâche ou un événement les conserve, avec leur "
            "statut"
        ),
        en=(
            "NOTHING in Athéna can EVER be DELETED here: a cancelled task or "
            "event is kept, with its status."
        ),
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
    # Lot 3b (BILL) falsified two promises and DELETED them: « it never
    # changes an invoice's status — voiding included » (update_invoice sets
    # envoyée / en_retard and voids) and « it never allocates an invoice
    # number » (create_invoice draws the year's next one). What stays true
    # is narrower, and each piece is its own promise below.
    Never(
        key="invoice_send",
        fr=(
            "<strong>envoyer</strong> une facture à qui que ce soit — la "
            "marquer «&nbsp;envoyée&nbsp;» n'envoie rien"
        ),
        en=(
            "It never SENDS an invoice to anyone: marking one envoyée "
            "sends nothing."
        ),
        # Sending an invoice is an email: the connector never imports the
        # email module (the client_message promise forbids it too — both
        # stay, each for the claim it backs).
        forbidden_modules=("utils.courriel",),
    ),
    Never(
        key="invoice_paid",
        fr=(
            "marquer une facture <strong>payée</strong> — seul un "
            "encaissement inscrit dans l'application le fait"
        ),
        en=(
            "It never marks an invoice payée: only a payment recorded in the "
            "application does."
        ),
        # A payload promise: update_invoice's status enum has no « payée »,
        # and the model's transitions never offer it (only record_payment's
        # automatic flip writes it — forbidden by « payment »).
        behavioural_test=(
            "tests/test_mcp_billing_writes.py::"
            "test_update_invoice_can_never_mark_an_invoice_paid"
        ),
    ),
    Never(
        key="invoice_sources",
        fr=(
            "écarter <strong>en silence</strong> une entrée de temps ou un "
            "déboursé nommé pour une facture — une seule source inutilisable "
            "fait refuser la facture entière"
        ),
        en=(
            "It never drops a named time entry or disbursement from an "
            "invoice silently: one unusable source refuses the whole "
            "invoice, and nothing is written."
        ),
        # create_invoice SKIPS an unusable source in silence unless told
        # otherwise — so every connector call must say so.
        required_keywords=(("create_invoice", "require_all_sources"),),
        behavioural_test=(
            "tests/test_mcp_billing_writes.py::"
            "test_every_connector_invoice_creation_requires_all_sources"
        ),
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
        key="uncancel",
        fr=(
            "<strong>défaire une annulation</strong> en silence — rouvrir "
            "une tâche annulée exige une demande expresse"
        ),
        en=(
            "It never silently undoes a cancellation: reopening an annulée "
            "task requires reopen_cancelled true."
        ),
        # A four-state toggle: it sends annulée AND terminée back to
        # à_faire, silently un-cancelling a cancelled task. Lot 1b's
        # reopen_task replaced the promise « never reopens a closed task »
        # (now false) with this narrower one, which stays true.
        forbidden=("toggle_task_complete",),
        behavioural_test=(
            "tests/test_mcp_agenda_writes.py::"
            "test_reopen_task_never_uncancels_without_the_explicit_flag"
        ),
    ),
    Never(
        key="client_message",
        # « Tiers », never « extérieur » (review of L8): the AGENDA block
        # discloses that an event's copy in the lawyer's OWN Outlook
        # calendar follows a reschedule or a cancellation (the mirror cron)
        # — an effect outside Athéna that reaches nobody else. What this
        # promise confines is what reaches ANOTHER person.
        fr=(
            "<strong>rédiger un message</strong> destiné à un client ou à "
            "un tiers — le seul effet qui atteigne un tiers est l'annulation "
            "Outlook d'une demande de rendez-vous Bookings refusée, dont le "
            "texte est fixe"
        ),
        en=(
            "It never composes a message to anyone outside the practice: "
            "its ONE effect reaching someone outside the practice is the "
            "Outlook cancellation a refused Bookings request sends, with a "
            "fixed text."
        ),
        # The Graph verbs that send or rewrite something in a mailbox or a
        # calendar, and the email module: no connector module and no
        # service a tool reaches may name them. The one outbound call the
        # connector reaches is services/rendez_vous → graph_calendrier
        # .annuler_reservation(gid, REFUS_MOTIF), whose text no argument
        # can carry — the behavioural test proves it.
        forbidden=("envoyer", "graph_post", "graph_patch", "graph_delete"),
        forbidden_modules=("utils.courriel",),
        behavioural_test=(
            "tests/test_mcp_hearing_writes.py::"
            "test_a_refusal_sends_only_the_fixed_cancellation_text"
        ),
    ),
    Never(
        key="document",
        # Lot 2A (T7) lifted a document's name, date, tags, folder and a
        # PRESUMED category; T8 (fill_gabarit, create_document) lifted
        # « never adds a new file to a dossier » — a generation or a copy
        # is a NEW document. What stays forbidden is changing the FILE of a
        # document that exists: the connector writes no byte itself (these
        # GCS verbs appear in no connector module nor any service it
        # reaches), and the two creators it reaches mint a fresh record and
        # write a NEW object, create-only (ingest's if_generation_match=0).
        # T9 (finalize_upload) files an upload the same way: a NEW document
        # under the id its ticket reserved, never an existing one. T10
        # (create_template, update_template) only READS a stored document:
        # its bytes are copied into a TEMPLATE object, the document's own
        # record and object untouched (tests/test_mcp_template_writes.py).
        # Lot 2B (create_template's `substitutions`) templatizes an
        # in-memory COPY of those bytes into the template object; the
        # source stays byte-identical (tests/test_mcp_templatize.py).
        fr=(
            "modifier le <strong>fichier</strong> d'un document existant "
            "— un projet, une copie ou un fichier téléversé est toujours un "
            "nouveau document ; la lecture de son contenu relève de l'accès "
            "en lecture ci-dessus"
        ),
        en=(
            "It never changes an existing document's FILE: a generated "
            "project, a copy or an upload is always a NEW document."
        ),
        forbidden=(
            "upload_from_file", "upload_from_string", "upload_from_filename",
            "rewrite", "compose",
        ),
        behavioural_test=(
            "tests/test_mcp_generation.py::"
            "test_no_generation_ever_touches_an_existing_document_file"
        ),
    ),
    Never(
        key="template_version",
        # Lot 2A (T11): the TEMPLATES family replaces a template's FILE
        # (update_template, finalize_upload in replace mode) — never in
        # place. The model writes each version to its own v{N} object,
        # create-only, and records a write-once versions/{N} entry; the one
        # in force before stays stored and restorable (web « Rétablir »).
        # No connector call can express an overwrite (the GCS byte verbs are
        # the « document » promise's), so the behaviour is pinned instead.
        fr=(
            "remplacer le fichier d'un gabarit <strong>sans en garder la "
            "version précédente</strong> — chaque version reste conservée, "
            "rétablissable dans l'application"
        ),
        en=(
            "It never replaces a template's file without keeping the "
            "previous one: every version stays stored, restorable in the "
            "application."
        ),
        behavioural_test=(
            "tests/test_mcp_template_writes.py::"
            "test_a_new_file_is_installed_as_a_new_version_the_old_one_kept"
        ),
    ),
    Never(
        key="link",
        # The documented exception to « no signed URL in tool output »
        # (plan D4, lot 2A T9): begin_upload's upload_url, a WRITE-only
        # resumable-session URI for ONE neutral staging object — the only
        # capability any result carries (tests/test_mcp_framework_guards
        # ._OUTPUT_NAME_EXEMPTIONS names it; tests/test_mcp_output_schemas
        # scans every real payload's VALUES with that single pair allowed).
        # The signers of the model layer are named so that no connector
        # module or reached service can ever mint a READ link.
        fr=(
            "obtenir un <strong>lien de lecture ou de téléchargement</strong> "
            "d'un fichier d'Athéna — le seul lien qu'il reçoit est celui d'un "
            "dépôt&nbsp;: "
            "en écriture seule, pour un seul fichier, versé seulement dans "
            "l'heure et seulement s'il est bien celui annoncé"
        ),
        en=(
            "It never hands out a link that reads or downloads a stored "
            "file, nor a storage path: the ONE link it mints is "
            "`begin_upload`'s `upload_url` — write-only, for one file, filed "
            "only within the hour and only if it is the file declared."
        ),
        forbidden=(
            "get_signed_url", "sign_blob_url", "generate_signed_url",
            "get_version_signed_url", "build_folder_zip_url",
        ),
        behavioural_test=(
            "tests/test_mcp_framework_guards.py::"
            "test_no_output_at_all_declares_a_url_or_a_storage_path"
        ),
    ),
    Never(
        key="confirm",
        # Split from « document » by lot 2A (T7): a category Claude sets is
        # PRESUMED (D15), exactly like an analysis, and the ONE way out of
        # « présumée » is the lawyer's own gesture — confirmer_categorie,
        # confirmer_analyse, or his edit of the analysis (update_analyse:
        # « éditer, c'est confirmer »).
        fr=(
            "<strong>confirmer</strong> une catégorie ou une analyse "
            "présumées — vous seul le faites, dans l'application"
        ),
        en=(
            "It never confirms a presumed category or analysis: only the "
            "lawyer does, in the application."
        ),
        forbidden=(
            "confirmer_categorie", "confirmer_analyse", "update_analyse",
        ),
    ),
    Never(
        key="active_template",
        # Lot 2A (T9, decision D11): the first connector tools that write a
        # template (finalize_upload creates one or installs a new version
        # of one). The designation of THE note-d'honoraires and THE
        # note-print template — printed on every client's invoice note —
        # stays the lawyer's gesture in the application: a special kind is
        # created NOT active, and a replacement never moves the
        # designation (update_template writes only what changed). T10's
        # create_template / update_template tools keep both rules: the
        # model's metadata whitelist has no designation key, and a kind
        # change on the designated template is refused.
        # Fixups of lot 2A: the lawyer can now also WITHDRAW a designation
        # (web « Retirer la désignation », clear_active_template) — forbidden
        # to the connector alike: undesignating the note-d'honoraires
        # template makes every invoice note refuse.
        fr=(
            "<strong>désigner le gabarit actif</strong> des notes "
            "d'honoraires ou de l'impression des notes, ni en retirer la "
            "désignation — vous seul le faites, dans l'application"
        ),
        en=(
            "It never designates the ACTIVE note-d'honoraires or note-print "
            "template, nor withdraws that designation: only the lawyer does, "
            "in the application."
        ),
        forbidden=("set_active_template", "clear_active_template"),
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
    is the reclassifiers' batch ceiling and ``series_max`` a series'
    occurrence ceiling (utils/recurrence), both read from the registry.
    """
    from mcp import tools as _tools  # lazy: mcp.tools imports this module

    return {
        "phase_bulk_max": _tools.PHASE_BULK_MAX,
        "series_max": _tools._SERIES_MAX,
        "write_families": families_for(SCOPE_WRITE),
        "comptabilite_families": families_for(SCOPE_COMPTABILITE),
        "nevers": [_never_fr(n, comptabilite_offered) for n in NEVERS],
        "write_summary": write_summary_fr(),
    }


# The paragraph about the ONE content-reading tool — a read, so no family,
# but the privilege warning belongs in the text every client model reads.
_READ_CONTENT_EN = (
    "READ-CONTENT: `get_document_text` reads a stored document's TEXT LAYER "
    "(PDF and .docx; take ids from list_documents or from the entity of a "
    "write that created a document; bounded per call — follow next_page; a "
    "template is not a document — list_templates describes its fields). A "
    "scanned page has no text layer and is reported honestly "
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
            "Omitted — where the tool allows it (replacing a note's text, "
            "editing the théorie de la cause, deciding a Bookings request or "
            "changing an invoice does not) — the tool still refuses a change "
            "landing between its own read and its commit."
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
        "and events it CREATES, and the text it APPENDS to or REPLACES in a "
        "note, also carry a dated « … par Claude le … » line; a Word "
        "document it generates or copies says « par Claude (connecteur) » "
        "in its provenance (`genere_depuis`)."
    )
    parts.append(_FORMATS_EN)
    return " ".join(parts)
