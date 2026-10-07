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
its ONE-LINE entry in the INSTRUCTIONS index (which must name every member
literally). The rules of each tool live in its DESCRIPTION, never here
(finitions, contracts-1): INSTRUCTIONS ride every ``initialize``, and a real
client cuts them at ~2 048 characters.

**NEVERS** are the promises. The ``in_core`` ones are stated by the SAFETY
CORE that opens INSTRUCTIONS (their ``en`` is a clause of its « NEVER » list);
the others follow the family index, one sentence each. Each one carries the
CALLS it forbids — names
(full-match patterns over identifiers) and modules — which
``tests/test_mcp_disclosure.py`` sweeps over the connector's syntax tree
(calls, attributes, names, imports, ``getattr`` constants — never string
literals, so this file, which names them only as strings, cannot trip it).
A never that no call can express (the stored compliance fields are a
PAYLOAD) is backed by the input properties no write tool may declare and by
a named behavioural test that must exist. A later lot that falsifies a never DELETES it here —
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

from mcp import SCOPE_WRITE


@dataclass(frozen=True)
class Family:
    """One write family: who may call it, and how it is described."""

    #: Stable identifier (lowercase), unique.
    key: str
    #: The heading INSTRUCTIONS use (« CREATE: … »).
    label: str
    #: The scope every member declares — ``athena:write`` for every family
    #: since 2026-10-05, when the lawyer removed ``athena:comptabilite``.
    scope: str
    #: The member tools, in the order the texts name them.
    tools: tuple[str, ...]
    #: The consent-screen partial, included inside the scope's block.
    consent_template: str
    #: This family's clause of the grant checkbox summary (static markup).
    checkbox_summary_fr: str
    #: The INSTRUCTIONS index line after « LABEL: » — every member named,
    #: and the one rule a caller must know before opening the tools; the
    #: rest is in each tool's description. May carry the fields of
    #: :data:`INDEX_FIELDS` (``{phase_bulk_max}``, ``{entry_bulk_max}``),
    #: filled from the registry at build time.
    instructions_en: str


@dataclass(frozen=True)
class Never:
    """One promise, with the mechanism that keeps it true."""

    key: str
    #: The consent-screen bullet (static markup, no trailing punctuation).
    fr: str
    #: The INSTRUCTIONS sentence — or, with ``in_core``, the CLAUSE the
    #: SAFETY CORE's « NEVER, whatever the tool: … » list carries (no
    #: subject, no trailing punctuation).
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
    #: A short « jamais … » clause for the checkbox summary, when this
    #: promise belongs there.
    summary_fr: str = ""
    #: Stated in the SAFETY CORE, within the first 2 048 characters of
    #: INSTRUCTIONS (``HEAD_LIMIT``; finitions, contracts-1): the promises
    #: a client that cuts the field must still read.
    in_core: bool = False
    # (Until 2026-10-05 a promise could also be ``accounting_only`` — shown
    # beside the separate accounting box and told only to a token holding
    # ``athena:comptabilite`` — and carry ``fr_comptabilite`` /
    # ``summary_fr_comptabilite`` variants for the screen that offered that
    # box. The lawyer removed the scope: every promise is now told to every
    # token, in one list.)


# ── The write families ──────────────────────────────────────────────────

FAMILIES: tuple[Family, ...] = (
    Family(
        key="create",
        label="CREATE",
        scope=SCOPE_WRITE,
        tools=(
            "create_note", "append_to_note", "create_task", "create_hearing",
            "create_time_entry", "create_expense",
            "create_time_entries_bulk", "create_expenses_bulk",
            "create_partie", "create_dossier", "complete_dossier",
            "record_signification", "record_prescription_event",
        ),
        consent_template="mcp/families/_create.html",
        checkbox_summary_fr=(
            "créer des notes, tâches, événements, temps et déboursés (un à "
            "un, ou par lots écrits en entier ou pas du tout), contacts et "
            "dossiers (un dossier neuf reçoit l'arborescence de classement "
            "par défaut), et compléter un dossier (champs encore vides, "
            "significations, événements de prescription)"
        ),
        instructions_en=(
            "`create_note`, `append_to_note`, `create_task`, "
            "`create_hearing`, `create_time_entry`, `create_expense` (their "
            "batch forms `create_time_entries_bulk`, `create_expenses_bulk`: "
            "up to {entry_bulk_max} rows, ALL OR NOTHING), `create_partie`, "
            "`create_dossier`; `complete_dossier` fills ONLY empty fields "
            "and refuses to overwrite; `record_signification` and "
            "`record_prescription_event` append to the dossier's registers."
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
            "`update_partie`, `update_dossier`, `update_time_entry`, "
            "`update_expense` REPLACE what you name (an entry only while "
            "un-invoiced); `complete_task` closes a task and never reopens a "
            "closed one — that is `reopen_task`."
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
            "`update_task`, `reopen_task`, `update_note`, `edit_analyse` (the "
            "théorie de la cause), `create_protocol`, `update_protocol`, "
            "`add_protocol_step`, `update_protocol_step`, `update_hearing`, "
            "`create_hearing_series`; closing or reopening cascades between a "
            "task, its step and the protocol."
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
            "`decide_rendez_vous` confirms or refuses a pending Bookings "
            "request (refusing: the outbound effect above); a CONFIRMED "
            "rendez-vous is never rescheduled or cancelled here — that is "
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
            "`set_time_entry_phase`, `set_expense_phase`, "
            "`set_time_entry_phase_bulk`, `set_expense_phase_bulk` (up to "
            "{phase_bulk_max} rows a call) change ONLY the litigation phase — "
            "the only writes that change a row WHILE it stays carried to an "
            "invoice (a void releases it instead)."
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
            "`import_invoice` recreates a previous system's invoice under ITS "
            "number and date: it NEVER allocates a number and lands in "
            "brouillon."
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
            "`create_invoice` issues a NEW brouillon (read `preview_invoice` "
            "first) and CONSUMES the year's next number for ever; "
            "`update_invoice` corrects ONLY a brouillon, marks envoyée or "
            "en_retard (sending NOTHING) or voids; `create_budget_version` "
            "adds a budget version (read `get_budget` first)."
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
            "— la catégorie est remplacée, sauf celle que vous avez choisie "
            "ou confirmée ; le niveau ne descend jamais)"
        ),
        instructions_en=(
            "`record_document_analysis` records a qualification: the CODE "
            "derives the category — never over the lawyer's — and a "
            "protection level only ever RISES."
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
            "`update_document`, `move_documents`, `manage_folder` file "
            "documents (a category set here stays PRESUMED); `fill_gabarit`, "
            "`create_document` make NEW Word documents; `begin_upload` then "
            "`finalize_upload` bring an outside file in."
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
        # No brace in an index line: build_instructions runs str.format over
        # it (for {phase_bulk_max}). The {{field}} syntax is create_template's
        # own description, which is never formatted (tests/test_mcp_generation
        # pins both).
        instructions_en=(
            "`create_template`, `update_template` register or correct a "
            "gabarit, a replaced file kept (templatizing with "
            "`substitutions`: read `preview_templatize` first); only the "
            "lawyer designates the ACTIVE ones."
        ),
    ),
    Family(
        key="dossiers",
        label="DOSSIERS",
        scope=SCOPE_WRITE,
        tools=("set_dossier_status", "update_dossier_party"),
        consent_template="mcp/families/_dossiers.html",
        checkbox_summary_fr=(
            "changer le statut d'un dossier — le fermer ou l'archiver retire "
            "ses tâches, notes et événements du téléphone, le rouvrir les y "
            "remet —, corriger les rôles ou l'avocat d'une partie au dossier, "
            "en détacher une (le contact reste) et rafraîchir les noms que le "
            "dossier garde de ses parties"
        ),
        instructions_en=(
            "`set_dossier_status` (fermé / archivé DRAINS its DavX5 "
            "collection) and `update_dossier_party` (a party's roles or "
            "lawyer; a detach removes a LINK — the contact stays)."
        ),
    ),
    Family(
        key="contacts",
        label="CONTACTS",
        scope=SCOPE_WRITE,
        tools=("update_partie_mandataire", "record_kyc_status"),
        consent_template="mcp/families/_contacts.html",
        checkbox_summary_fr=(
            "tenir les mandataires d'un contact (en ajouter, corriger, "
            "détacher — le contact mandataire reste), et inscrire une "
            "vérification d'identité ou de conflits d'intérêts comme "
            "PRÉSUMÉE — «&nbsp;à confirmer&nbsp;» sur la fiche jusqu'à votre "
            "confirmation, jamais par-dessus une vérification que vous avez "
            "vous-même décidée"
        ),
        instructions_en=(
            "`update_partie_mandataire` and `record_kyc_status`, which "
            "INSCRIBES a PRESUMED check — it counts as NOT done until the "
            "lawyer confirms it."
        ),
    ),
    # Lot 5b — ACCOUNTING (plan D1, D2, D14, D16). Under its own scope,
    # athena:comptabilite, and its own consent box until 2026-10-05, when
    # the lawyer removed both: a write family like the others since.
    Family(
        key="accounting",
        label="ACCOUNTING",
        scope=SCOPE_WRITE,
        tools=(
            "record_trust_entry", "record_admin_entry", "update_admin_entry",
            "clear_register_entries", "reverse_register_entry",
        ),
        consent_template="mcp/families/_comptabilite.html",
        # Lot 5, step 5: « des recettes, des déboursés et des paiements
        # d'honoraires — chacun inscrit … la recette au compte
        # d'administration » bound « chacun » to all three, and a trust
        # recette or déboursé inscribes nothing at administration. Only the
        # fee payment does. 2026-10-07: the administration ledger's four
        # kinds outside the firm's results (models/admin_ledger
        # .NON_RESULT_KINDS) — the lawyer's prélèvement and apport, a
        # virement interne either way — are named beside the others.
        checkbox_summary_fr=(
            "inscrire au fidéicommis des recettes et des déboursés, et des "
            "paiements d'honoraires — dont chacun inscrit, dans la même "
            "opération, la recette au compte d'administration et le "
            "paiement sur la facture —, inscrire au compte "
            "d'administration des dépenses, d'autres recettes, des "
            "encaissements de facture (qui inscrivent le paiement sur la "
            "facture), des paiements de carte, vos prélèvements et vos "
            "apports, et des virements internes vers ou depuis un compte "
            "hors de ce registre, corriger une écriture d'administration "
            "tant qu'elle reste modifiable, compenser des écritures à la "
            "date du relevé bancaire et contre-passer une écriture"
        ),
        # The writers' own rule, verbatim (review of E3): the index line once
        # read « what the bank shows », which excludes a cheque just written
        # — recorded en_circulation, on no statement until it clears.
        instructions_en=(
            "`record_trust_entry`, `record_admin_entry`, "
            "`update_admin_entry`, `clear_register_entries`, "
            "`reverse_register_entry` (read: `get_admin_ledger`): record ONLY "
            "movements that HAPPENED at the bank, at their date, each "
            "confirmed with the user; an idempotency_key is REQUIRED (refused "
            "when the replay "
            "store is unreadable); an uncertain outcome → re-read before "
            "retrying with the SAME key."
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

# The register writers NO tool may reach, whatever its scope (lot 5b): the
# reconciliations, the accounts, the inter-dossier transfer and the receipt
# stay the lawyer's, in the application. Named here rather than by module:
# the connector reads and writes the registers through
# services/comptabilite, so importing the models is legitimate; calling one
# of these is not. (Until lot 5b the list also held the entry writers —
# create/update/clear/reverse, the card payment — and backed a promise that
# NO tool touched the registers; the ACCOUNTING family reaches those now,
# and what keeps them away from every other tool is a derived reach test,
# tests/test_mcp_accounting.py::
# test_only_the_accounting_tools_reach_a_register_writer.) Lot 5, step 5 split the one list
# three ways — deleting an entry, the reconciliations and accounts, the
# inter-dossier transfer —, each the forbidden calls of its OWN promise, so
# the screen states each impossibility where its sweep backs it.
_REGISTER_DELETERS: tuple[str, ...] = (
    "delete_transaction", "delete_card_payment",
)
_REGISTER_SETUP_WRITERS: tuple[str, ...] = (
    "create_reconciliation", "complete_reconciliation",
    "delete_reconciliation", "create_account", "update_account",
    "attach_receipt",
)
_REGISTER_TRANSFER_WRITERS: tuple[str, ...] = (
    "create_inter_dossier_transfer",
)

NEVERS: tuple[Never, ...] = (
    Never(
        key="delete",
        # Lot 1b made cancelling a CAPABILITY (a task, an event, a Bookings
        # request): the promise is about Athéna's records, which a
        # cancellation keeps — the Outlook side of a cancelled event or of a
        # refused request is disclosed by its family, beside the capability.
        # Lot 4b: detaching a party from a dossier erases an array ENTRY —
        # a LINK, never a record: the contact stays, and the model journals
        # the detach in audit_events (dossier_party), which list_deletions
        # reads. Said beside the promise it narrows.
        fr=(
            "<strong>supprimer</strong> quoi que ce soit dans Athéna — "
            "annuler une tâche ou un événement les conserve, avec leur "
            "statut&nbsp;; détacher une partie d'un dossier ou un mandataire "
            "d'un contact retire un lien, le contact reste"
        ),
        en=(
            "DELETE anything (a cancelled item is kept; detaching a party or "
            "a mandataire removes a LINK — the contact stays)"
        ),
        in_core=True,
        forbidden=(r"delete_\w+", r"record_deletion"),
        # The protocol layer deletes its own bookkeeping (an expired OAuth
        # client, a released idempotency claim) — never a user record. The
        # tool code may not call a bare `.delete()` at all.
        forbidden_in_tool_code=("delete",),
        summary_fr="de suppression",
    ),
    Never(
        key="payment",
        # Lot 5, step 5: « sauf par une écriture aux registres » named no
        # entry. A payment exists as exactly TWO register entries, and the
        # screen names both — and says who writes it onto the invoice.
        # 2026-10-05: the accounting tools are write tools like the others
        # (the separate grant left), so this wording — until then the
        # variant shown beside the accounting box — is the only one.
        fr=(
            "inscrire un <strong>paiement</strong> autrement que par une "
            "écriture aux registres comptables — un encaissement au compte "
            "d'administration ou un paiement d'honoraires au fidéicommis, "
            "qui inscrit lui-même le paiement sur la facture"
        ),
        # A SAFETY CORE clause since contracts-1, part 2: only a register
        # entry records a payment — written by the REGISTER, in the entry's
        # own transaction; record_trust_entry and record_admin_entry name
        # which.
        en=(
            "record a payment outside the accounting registers"
        ),
        in_core=True,
        # Lot 5a (step 2): the ledger now STAGES the payment itself, through
        # the pure payment_updates — swept too, so a handler cannot build a
        # payment write of its own around the ledger's back. The two retired
        # projection helpers and their deleted module stay listed: a helper
        # reborn under the same name would be the same trap. Lot 5b's
        # accounting tools reach a payment ONLY through the registers'
        # models (services/comptabilite → models/admin_ledger,
        # models/fee_payment), so the sweep stays global — and green.
        forbidden=("record_payment", "payment_updates", "projeter_paiement",
                   "reduire_paiement"),
        forbidden_modules=("services.encaissements",),
        summary_fr=(
            "de paiement autrement que par une écriture aux registres "
            "comptables (un encaissement ou un paiement d'honoraires)"
        ),
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
            "SEND an invoice to anyone (marking one envoyée sends nothing)"
        ),
        in_core=True,
        # Sending an invoice is an email: the connector never imports the
        # email module (the client_message promise forbids it too — both
        # stay, each for the claim it backs).
        forbidden_modules=("utils.courriel",),
    ),
    Never(
        key="invoice_paid",
        fr=(
            "marquer une facture <strong>payée</strong> par un changement de "
            "statut — seul un paiement inscrit aux registres comptables le "
            "fait"
        ),
        en=(
            "mark an invoice payée by a status change"
        ),
        in_core=True,
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
            "invoice silently: one unusable source refuses the whole invoice."
        ),
        # create_invoice SKIPS an unusable source in silence unless told
        # otherwise — so every connector call must say so.
        required_keywords=(("create_invoice", "require_all_sources"),),
        behavioural_test=(
            "tests/test_mcp_billing_writes.py::"
            "test_every_connector_invoice_creation_requires_all_sources"
        ),
    ),
    # Lot 4b DELETED « a dossier's status is set at CREATION and can never be
    # changed here »: set_dossier_status changes it, through the same drain
    # service (services/dossier_dav) the application's form uses. What stays
    # true — update_dossier and complete_dossier never write a status, so no
    # status reaches the model without its drain — is pinned by
    # tests/test_mcp_import.py (the tripwire) and tests/test_mcp_dossier_
    # writes.py.
    # Lot 4b split « trust_identity »: record_kyc_status INSCRIBES a
    # compliance check (update_kyc_status, source « mcp »), which falsified
    # « never touches identity verification or conflict checks ». The trust
    # half stays whole; what stays true of the compliance half is its own
    # promise below (« kyc »).
    # 2026-10-05 DELETED « trust » — « without the separate
    # athena:comptabilite grant it never writes to trust accounting »: the
    # lawyer removed that grant, and the ACCOUNTING family is a write family
    # like the others. What the register tools still never do is the precise
    # list below (« register_delete » … « account_number »), told to every
    # token now.
    Never(
        key="kyc",
        # D7: a check the connector inscribes is PRESUMED — the model stamps
        # its source « mcp », the fiche shows « à confirmer », the coverage
        # report keeps it open — and the ONE way out is the lawyer's
        # « Confirmer » (models/partie.confirm_kyc_status, which refuses the
        # connector as a writer). A check the lawyer decided or confirmed is
        # refused to an « mcp » write by the model (utils/kyc
        # .apply_status_transition, update_kyc_status) — the behaviour is
        # pinned; every connector call names its source (required keyword),
        # and no write tool takes the stored compliance fields as input.
        fr=(
            "<strong>confirmer</strong> une vérification d'identité ou de "
            "conflits d'intérêts, ni modifier une vérification que vous avez "
            "décidée ou confirmée — ce que Claude inscrit reste "
            "«&nbsp;à confirmer&nbsp;»"
        ),
        en=(
            "CONFIRM an identity or conflict check, or change one the lawyer "
            "decided or confirmed"
        ),
        in_core=True,
        forbidden=("confirm_kyc_status", "link_kyc_document"),
        required_keywords=(("update_kyc_status", "source"),),
        forbidden_inputs=tuple(
            ("*", field) for field in (
                "identity_verified", "identity_verified_date",
                "identity_verified_notes", "identity_verified_source",
                "identity_verified_confirmed_at",
                "identity_verified_confirmed_by", "conflict_check",
                "conflict_check_date", "conflict_check_notes",
                "conflict_check_source", "conflict_check_confirmed_at",
                "conflict_check_confirmed_by", "kyc_document_ids",
            )
        ),
        behavioural_test=(
            "tests/test_mcp_contact_writes.py::"
            "test_record_kyc_status_never_changes_or_confirms_the_lawyer_s_attestation"
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
            "compose a message to anyone outside the practice"
        ),
        in_core=True,
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
            "It never replaces a template's file without keeping the previous "
            "one, restorable in the application."
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
            "It never hands out a link that reads or downloads a stored file, "
            "nor a storage path: its ONE link is `begin_upload`'s "
            "`upload_url` — write-only, for one declared file within the "
            "hour."
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
            "confirm a presumed category or analysis"
        ),
        in_core=True,
        forbidden=(
            "confirmer_categorie", "confirmer_analyse", "update_analyse",
        ),
        # Review of D25 (2026-09-29): the sweep above forbids the three
        # gestures by NAME, and the first version of D25 breached the
        # promise without naming any — the connector's own
        # record_document_analysis KEPT `confirme: true` on a run the lawyer
        # never read. The behaviour is pinned too.
        behavioural_test=(
            "tests/test_document_analyse_d25.py::"
            "test_no_connector_write_confirms_an_analysis_or_a_presumed_category"
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
            "It never designates, nor withdraws, the ACTIVE note-d'honoraires "
            "or note-print template: only the lawyer does."
        ),
        forbidden=("set_active_template", "clear_active_template"),
    ),
    # ── Lot 5 — what the ACCOUNTING tools never do ───────────────────────
    # Until 2026-10-05 shown only beside the separate accounting box and
    # told only to a token holding athena:comptabilite (``accounting_only``);
    # the lawyer removed that scope, so every token is told them now. Swept
    # over every connector module and reached service, as ever.
    #
    # Step 5 made this the FINAL set, the precise list that replaced
    # « toucher au fidéicommis » (plan D1, D14): no entry deleted — the
    # correction each register allows, said —, no reconciliation and no
    # account, no transfer between dossiers, no cash withdrawal, no fee
    # payment on a paper, unsent or provision-imputing invoice, no bank
    # number shown. The lawyer's decisions of 2026-09-29 added two: no fee
    # payment on another client's invoice (D21, in « fee_invoice ») and
    # none to a payee other than the lawyer or his firm (D23, « fee_payee »). Lot 5b had two broader bullets (« register_setup »
    # carried the deletion and the transfer; « trust_withdrawal » the
    # invoice rules) and no word of the UNSENT invoice the model refuses.
    Never(
        key="register_delete",
        fr=(
            "<strong>supprimer</strong> une écriture — au fidéicommis, une "
            "erreur ne se corrige que par une "
            "<strong>contre-passation</strong>, l'original et sa "
            "contre-passation restant au registre pour toujours&nbsp;; au "
            "compte d'administration, une écriture se corrige tant qu'elle "
            "reste modifiable, puis par une contre-passation"
        ),
        en=(
            "It never deletes a register entry: a trust entry is corrected "
            "only by a reversal (both kept for good), an administration "
            "entry with `update_admin_entry` while editable, by a reversal "
            "afterwards."
        ),
        # The registers' deleters, named beside the « delete » promise's
        # pattern (which matches them too): a promise about ENTRIES keeps
        # its own sweep.
        forbidden=_REGISTER_DELETERS,
    ),
    Never(
        key="register_setup",
        fr=(
            "<strong>commencer, compléter ou abandonner une "
            "conciliation</strong>, ni créer ou modifier un "
            "<strong>compte</strong>, ni joindre un reçu — vous seul le "
            "faites, dans l'application"
        ),
        en=(
            "It never starts, completes or abandons a reconciliation, creates "
            "or modifies an account, or attaches a receipt: only the lawyer "
            "does, in the application."
        ),
        forbidden=_REGISTER_SETUP_WRITERS,
        summary_fr="de conciliation ni de compte",
    ),
    Never(
        key="register_transfer",
        fr=(
            "<strong>virer des fonds</strong> du fidéicommis d'un dossier à "
            "un autre, ni contre-passer un volet d'un tel virement — vous "
            "seul le faites, dans l'application"
        ),
        en=(
            "It never transfers trust funds between dossiers, nor reverses "
            "a leg of such a transfer: only the lawyer does, in the "
            "application."
        ),
        # The writer is swept; the reversal of a leg is refused by the
        # handler before the model (a leg's reversal moves one client's
        # funds back to another) — pinned by its behavioural test.
        forbidden=_REGISTER_TRANSFER_WRITERS,
        behavioural_test=(
            "tests/test_mcp_accounting.py::"
            "test_a_transfer_between_dossiers_is_never_reversed_here"
        ),
        summary_fr="de virement entre dossiers",
    ),
    Never(
        key="trust_withdrawal",
        fr=(
            "retirer du fidéicommis <strong>en espèces</strong> (art. 57 — "
            "le remboursement en espèces de l'art. 72 s'inscrit dans "
            "l'application)"
        ),
        en=(
            "It never withdraws trust funds in cash (art. 57 — the art. 72 "
            "cash refund is recorded in the application)."
        ),
        # The model refuses a cash withdrawal unless it cites the art. 72
        # cash receipt — so no tool may even DECLARE that input.
        forbidden_inputs=(("*", "cash_receipt_id"),),
        behavioural_test=(
            "tests/test_mcp_accounting.py::"
            "test_record_trust_entry_never_withdraws_cash_nor_pays_a_paper_or_provision_invoice"
        ),
        summary_fr="de retrait en espèces",
    ),
    Never(
        key="fee_invoice",
        fr=(
            "appuyer un <strong>paiement d'honoraires</strong> sur une "
            "facture papier, sur une facture <strong>pas encore "
            "envoyée</strong>, sur une facture qui impute une "
            "<strong>provision</strong> ou sur la facture d'un "
            "<strong>autre client</strong> que celui dont les fonds sortent "
            "(ni sur celle qui n'en nomme aucun, dans un dossier qui en "
            "compte plusieurs)"
        ),
        en=(
            "It never backs a fee payment with a paper invoice, with an "
            "invoice not yet sent, with an invoice that imputes a "
            "provision, or with another client's invoice than the one whose "
            "funds leave trust (nor one naming no client, in a dossier of "
            "several)."
        ),
        # The model refuses an unsent invoice (facture_non_émise), a
        # provision (facture_avec_provision) and — decision D21, 2026-09-29 —
        # another client's invoice (facture_autre_client) — or one naming
        # no client in a dossier of several (facture_sans_client) — on
        # every path, and
        # the external-invoice path unless its caller turns it on — the web
        # form does, the connector never (allow_external_ref=False): no
        # tool may even DECLARE the paper-invoice input. The same test pins
        # all five refusals.
        forbidden_inputs=(("*", "invoice_external_ref"),),
        behavioural_test=(
            "tests/test_mcp_accounting.py::"
            "test_record_trust_entry_never_withdraws_cash_nor_pays_a_paper_or_provision_invoice"
        ),
        summary_fr=(
            "de paiement d'honoraires sur une facture papier, non envoyée, "
            "qui impute une provision ou adressée à un autre client"
        ),
    ),
    # Decision D23 (2026-09-29, art. 58 — « chèque tiré à l'ordre de
    # l'avocat »): the payee of a fee payment is the lawyer or his firm, as
    # the firm profile names them — the MODEL refuses any other
    # (models/fee_payment, web included), and the connector's handler
    # repeats it naming `counterparty`.
    Never(
        key="fee_payee",
        fr=(
            "faire un <strong>paiement d'honoraires</strong> à l'ordre de "
            "quelqu'un d'autre que <strong>vous ou votre cabinet</strong>, "
            "tels que les nomme le profil du cabinet (art.&nbsp;58)"
        ),
        en=(
            "It never makes a fee payment to anyone but the lawyer or his "
            "firm, as the firm profile names them (art. 58)."
        ),
        behavioural_test=(
            "tests/test_mcp_accounting.py::"
            "test_d23_the_payee_is_the_lawyer_or_his_firm"
        ),
        summary_fr="de paiement d'honoraires à l'ordre d'un autre que vous ou votre cabinet",
    ),
    Never(
        key="account_number",
        fr=(
            "afficher un <strong>numéro de compte</strong> bancaire ou de "
            "transit"
        ),
        en="It never shows a bank transit or account number.",
        behavioural_test=(
            "tests/test_mcp_accounting.py::"
            "test_no_accounting_payload_ever_carries_a_transit_or_account_number"
        ),
    ),
)


# ── Derivations ─────────────────────────────────────────────────────────

#: The ``str.format`` fields an index line (``Family.instructions_en``) may
#: carry, each filled by :func:`build_instructions` from the registry — the
#: reclassifiers' and the bulk creators' batch ceilings. No other brace may
#: stand in an index line (tests/test_mcp_generation pins it).
INDEX_FIELDS: tuple[str, ...] = ("phase_bulk_max", "entry_bulk_max")


def write_tools() -> frozenset[str]:
    """Every write tool — the union of the families (``mcp.tools.WRITE_TOOLS``)."""
    return frozenset(name for family in FAMILIES for name in family.tools)


def families_for(scope: str) -> tuple[Family, ...]:
    """The families under *scope* that have at least one tool, in order."""
    return tuple(f for f in FAMILIES if f.scope == scope and f.tools)


def _never_fr(never: Never) -> Markup:
    # Static constants of this module, never request data: safe to mark.
    return Markup(never.fr)  # nosec B704 — module constant, no user input


def _summary(clauses: list[str], nevers: list[str]) -> Markup:
    body = "&nbsp;; ".join(clauses)
    body = body[:1].upper() + body[1:] + "."
    if nevers:
        body += " Jamais " + ", jamais ".join(nevers) + "."
    return Markup(body)  # nosec B704 — module constants, no user input


def write_summary_fr() -> Markup:
    """The write checkbox's summary: every write family's clause, then the
    short « jamais » clauses — derived, so it names exactly what is granted."""
    nevers = [n.summary_fr for n in NEVERS if n.summary_fr]
    return _summary([f.checkbox_summary_fr for f in families_for(SCOPE_WRITE)],
                    nevers)


def consent_context() -> dict:
    """What ``templates/mcp/consent.html`` renders from the registry.

    ``write_families`` are included, in order, inside the write block —
    the ACCOUNTING family's among them since 2026-10-05, when its separate
    box left; ``nevers`` are the bullets of its « jamais » list (every
    promise, one list); ``write_summary`` is the grant checkbox's summary.
    ``phase_bulk_max`` is the reclassifiers' batch ceiling,
    ``entry_bulk_max`` the bulk creators' and ``series_max`` a series'
    occurrence ceiling (utils/recurrence), all read from the registry;
    ``register_clear_max`` is a clearing's batch ceiling.
    """
    from mcp import tools as _tools  # lazy: mcp.tools imports this module

    return {
        "phase_bulk_max": _tools.PHASE_BULK_MAX,
        "entry_bulk_max": _tools.ENTRY_BULK_MAX,
        "series_max": _tools._SERIES_MAX,
        "register_clear_max": _tools.REGISTER_CLEAR_MAX,
        "write_families": families_for(SCOPE_WRITE),
        "nevers": [_never_fr(n) for n in NEVERS],
        "write_summary": write_summary_fr(),
    }


# ── INSTRUCTIONS (finitions, contracts-1; the 2 048-character head) ───────
#
# A real client CUTS the field: Claude Code truncates the server
# instructions — and each tool description — at 2 048 characters
# (CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH), and this connector's production
# text reached a session cut after its 2 048th character while it had grown
# to 22-26 KB of family prose with the protocol rules last. Part 2 of
# contracts-1 moved a SAFETY CORE to the front; the CONVENTIONS (money,
# dates, ids, provenance) still came LAST, after ~4 KB of index, so every
# truncating client lost them. The text now opens on a HEAD — the SAFETY
# CORE (what can never happen, the one outbound effect, confirm-before-
# writing, the idempotency and etag discipline, re-read before retrying)
# then the CONVENTIONS — complete within the first 2 048 characters
# (HEAD_LIMIT); then the index (one line per family naming its tools),
# READ-CONTENT, the scope sentence and the remaining promises. The detailed
# rules live in each tool's DESCRIPTION, which a model reads before it calls
# the tool. tests/test_mcp_descriptor_budget.py pins the head's position and
# completeness, and the total size (INSTRUCTIONS_CAP) of every variant.

#: What a client cutting the field must still have read: the SAFETY CORE
#: and the CONVENTIONS, whole, within this many characters (Claude Code's
#: default cut — measured in characters, as the client counts).
HEAD_LIMIT = 2_048

# The opening of the core — the product, then what the core is for.
_CORE_LEAD_EN = (
    "Pallas Athena (single-user Quebec civil litigation practice manager). "
    "SAFETY CORE — read before ANY write."
)

# The one effect reaching outside the practice, stated with the promises —
# « Only ONE »: the sentence is a promise that nothing ELSE leaves the
# practice, and « ONE effect reaches » alone reads as « one such effect ».
_CORE_OUTBOUND_EN = (
    "Only ONE effect reaches anyone outside the practice: "
    "`decide_rendez_vous` refusing a Bookings request cancels the client's "
    "Outlook meeting, notifying them."
)

# The write protocol: confirm, idempotency (in flight / uncertain /
# interrupted / committed), the etag, and re-read before any retry.
# Compressed 2026-09-30 to leave the CONVENTIONS room within the head —
# every rule kept, the two explanations left to the refusal messages, each
# of which carries its own: « une étape qui la suit a échoué » (tools.
# CommittedWriteError) and « il ne peut plus être en cours » (write_support.
# _refuse_pending — added to that refusal the same day, since it is what
# makes the re-read reliable before a NEW key). « …, never blindly » left
# too: it restated « redo the write on the current record », which already
# excludes resending the stale arguments.
_CORE_PROTOCOL_EN = (
    "Writes are permanent and may sync to the lawyer's phone: read first; "
    "confirm with the user unless a standing instruction authorizes it. "
    "Pass an `idempotency_key` on EVERY write (24 h replay, never a second "
    "write): refused as still in flight → wait, then the SAME key, never a "
    "new one; UNCERTAIN → re-read, retry only with the SAME key; refused as "
    "INTERRUPTED → re-read, a NEW key only if nothing was written; "
    "« ENREGISTRÉE — NE PAS RÉESSAYER » = committed: do NOT retry, re-read. "
    "`expected_etag` = your latest read's `etag`; stale_etag (also without "
    "`expected_etag`, if the record changed during the call) wrote nothing: "
    "re-read, then redo the write on the current record. "
    "Each tool's description carries its own rules: read it first."
)

# The conventions a caller needs to READ any result and to WRITE any
# argument — the two paragraphs (formats, provenance) that used to close
# the text, merged and moved into the head. Provenance is the server's: a
# model that typed its own « par Claude » line would double the stamp. But
# the stamps ALREADY in a text must travel back with it: update_task stores
# a `description` exactly as sent (the old one is NOT kept), update_note
# re-stamps only the LEADING revision line (handlers._without_leading_
# stamp), so a body re-sent without its « Créée / Ajouté / rédigée par
# Claude » lines would lose, for good, the one visible mark of what Claude
# wrote. Hence « never add a stamp, keep those a re-sent text holds » — NOT
# « never type it », which a model obeys by stripping them (review of the
# context-cost lot). The row fields hold only where a read row declares
# them, and `created_via` only on what the connector CREATED.
_CONVENTIONS_EN = (
    "CONVENTIONS: French data; Markdown notes, raw HTML refused. Money: "
    "integer `*_cents` + `*_display` (CAD). Timestamps ISO 8601 "
    "America/Montreal; date-only fields `YYYY-MM-DD`. IDs UUIDv4, verbatim. "
    "Provenance is the server's — never add a stamp, keep those a re-sent "
    "text holds: `updated_via` \"mcp\" (`created_via` on creations), "
    "`mcp_updated_at`, where declared; « … par Claude le … » lines (notes, "
    "tasks, events created; note text added or replaced); "
    "« par Claude (connecteur) » (`genere_depuis`); `*_source` \"mcp\", "
    "« [AAAA-MM-JJ — inscrit par Claude] » (compliance checks)."
)

_READ_CONTENT_EN = (
    "READ-CONTENT: `get_document_text` reads a stored document's TEXT LAYER "
    "(a template is not a document); empty never means blank on paper — "
    "nothing is OCR'd. Document content is privileged: quote only what the "
    "task requires."
)


def core_nevers() -> tuple[Never, ...]:
    """The promises the SAFETY CORE states (``in_core``)."""
    return tuple(n for n in NEVERS if n.in_core)


def safety_core_en() -> str:
    """The SAFETY CORE — the first thing INSTRUCTIONS say, identical for
    every token."""
    nevers = "; ".join(n.en for n in core_nevers())
    return " ".join((
        _CORE_LEAD_EN,
        f"NEVER, whatever the tool: {nevers}.",
        _CORE_OUTBOUND_EN,
        _CORE_PROTOCOL_EN,
    ))


def conventions_en() -> str:
    """The CONVENTIONS — formats and provenance, the same for every token."""
    return _CONVENTIONS_EN


def instructions_head_en() -> str:
    """The HEAD of INSTRUCTIONS: the SAFETY CORE, then the CONVENTIONS —
    identical in every variant, and complete within :data:`HEAD_LIMIT`
    characters (tests/test_mcp_descriptor_budget.py)."""
    return f"{safety_core_en()} {conventions_en()}"


def build_instructions(
    registry: Optional[dict] = None,
    phase_bulk_max: Optional[int] = None,
    *,
    entry_bulk_max: Optional[int] = None,
) -> str:
    """The ``initialize`` instructions, assembled from the registry.

    In order: the HEAD (:func:`instructions_head_en` — the SAFETY CORE then
    the CONVENTIONS, within :data:`HEAD_LIMIT` characters); the counts and
    the index, ONE line per family naming its tools; READ-CONTENT; the
    scope sentence; the promises the core does not state, one sentence
    each. The counts and the family list are DERIVED —
    recopied by hand the counts went stale twice, and a model told « 29
    read » looks for tools that are not there. The arguments exist for
    tests; by default the live registry is read (lazily: ``mcp.tools``
    imports this module).

    ONE text for every token since 2026-10-05: the variant a token holding
    ``athena:comptabilite`` read (the ACCOUNTING family, its read and the
    promises that grant kept) left with the scope — every family and every
    promise is in it.
    """
    if registry is None or phase_bulk_max is None or entry_bulk_max is None:
        from mcp import tools as _tools

        registry = _tools.TOOLS if registry is None else registry
        phase_bulk_max = (
            _tools.PHASE_BULK_MAX if phase_bulk_max is None else phase_bulk_max
        )
        entry_bulk_max = (
            _tools.ENTRY_BULK_MAX if entry_bulk_max is None else entry_bulk_max
        )

    writes = write_tools()
    families = [f for f in FAMILIES if f.tools]
    reads = len([n for n in registry if n not in writes])
    parts = [
        instructions_head_en(),
        f"TOOLS: {reads} tools read; {len(writes)} write, in "
        f"{len(families)} families:",
    ]
    for family in families:
        parts.append(
            f"{family.label}: "
            + family.instructions_en.format(
                phase_bulk_max=phase_bulk_max, entry_bulk_max=entry_bulk_max)
        )
    parts.append(_READ_CONTENT_EN)
    parts.append(
        "Write tools appear only when the lawyer granted the `athena:write` "
        "scope."
    )
    parts.extend(n.en for n in NEVERS if not n.in_core)
    return " ".join(parts)
