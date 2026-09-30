"""The disclosure registry (mcp/disclosure.py) — and the code that keeps it true.

Two properties are pinned here, both DERIVED:

1. **The registry is the whole truth about the families.** FAMILIES
   partition WRITE_TOOLS (which is derived from them), every member's
   declared scope is its family's, and INSTRUCTIONS — assembled from the
   registry — name every write tool, with counts computed from the registry,
   never typed.

2. **Every « never » is backed by the code.** Each NEVER carries the calls
   it forbids; this module walks the SYNTAX TREE of every connector module
   (``mcp/*.py`` but the registry itself) — calls, attributes, names,
   imports, ``getattr`` constants — and fails on any reference. String
   literals and comments are invisible to the walk by construction, which
   is why the registry can NAME a forbidden function in order to forbid it
   (a raw-text sweep would have matched the registry itself). A promise no
   call can express (the stored compliance fields are a PAYLOAD) is backed
   by the input properties no write tool may declare, and by a behavioural
   test that must EXIST.

The consent screen's rendering is pinned in test_mcp_oauth.py (where the
consent-flow fixtures live): every family partial and every NEVER bullet
reach the page, and no known false claim does.
"""

import ast
import os
import pathlib
import re
import sys
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp
    import mcp.disclosure as disclosure
    import mcp.endpoint as endpoint
    import mcp.tools as tools

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
_TEMPLATES = _ATHENA / "templates"

# Claims that were once on a surface and were FALSE. They must never come
# back — on the consent screen (test_mcp_oauth), in INSTRUCTIONS, in a
# handler warning or in a tool description. Case-insensitive substrings; the
# patterns allow the corrected continuation (or, for « signée », spare
# « consignée » through the word boundary).
KNOWN_FALSE_CLAIMS: tuple[str, ...] = (
    "se libère",                           # voiding does NOT free the number
    "frees the number",
    "chaque écriture est horodatée",       # only CREATED notes/tasks/events
                                           # carry a dated mention
    "can never edit or delete the entry",  # update_time_entry / update_expense
    "re-opens the linked step",            # complete_task now refuses it
    # create_note's stamp is « Note rédigée par Claude le … »; « Ajouté par
    # Claude » is append_to_note's separator.
    "« ajouté par claude » provenance line",
    # Cancelling a linked task triggers NO cascade (models/task
    # ._sync_protocol_step acts on « terminée » only): only TERMINATING it
    # completes the step.
    "clore une tâche rattachée à une étape de protocole complète",
    # Lot 1b: notes, tasks and the théorie de la cause became editable, and
    # reopening a task became a tool. Each of these once stood on a surface
    # and is false now.
    "cannot edit or delete it afterwards",           # create_note
    "is edited only in the app",                     # append_to_note
    "readable but read-only",                        # list_notes/get_note
    "the only status change this connector can make",  # complete_task
    "can close it with complete_task but can never edit",  # create_task
    "reopening a task is done in the application",
    "rouvrir une tâche se fait dans l'application",
    "jamais rouvrir une tâche close",                # consent, CORRECT
    "ajout à la fin — jamais de modification",       # consent, CREATE
    "en lecture seule via le connecteur",            # append_to_note refusal
    # update_note (revue L5): only a « <…> » span is refused — the sanitizer
    # deletes exactly that; a lone « < » is stored intact.
    "unpaired angle brackets are refused",
    # Lot 1b (L6): create_protocol exists — and record_signification had
    # made « cannot … file a signification » false since July 2026.
    "cannot create a protocol",
    "cannot file a signification",
    # Lot 1b (L7): update_hearing edits an event — create_hearing's old
    # description said the connector never could — and decide_rendez_vous
    # cancels a client's Outlook meeting: « nothing outbound » is false.
    "this connector can never edit or delete it",
    "the connector sends nothing outside",
    "n'envoie jamais rien",
    # Lot 1b (L8, the text sweep): update_note replaces a note's text, so
    # an append CAN be undone here; update_dossier (lot Q) replaces a set
    # dossier value, so neither « the lawyer's act, in the app » nor « never
    # overwritten by the connector » holds; and an auto-closed protocol is
    # reopened by reopen_task, not only in the application.
    "cannot be undone through this connector",
    "the lawyer's act, in the app",
    "jamais écrasés par le connecteur",
    "rouvrez-le dans l'application si ce n'était pas voulu",
    "nothing outbound at all",
    # Review of L8: the AGENDA block of the SAME screen says a cancelled or
    # moved event's copy in the lawyer's Outlook calendar follows (the
    # mirror) — an effect outside Athéna. « Le seul effet extérieur » beside
    # it was false; what the promise confines is what reaches a THIRD party.
    "le seul effet extérieur possible",
    # Lot 1 completeness review: the same claim in English, in the BOOKINGS
    # paragraph of INSTRUCTIONS — beside AGENDA's « status annulée removes
    # the event's Outlook copy ».
    "the connector's only outbound effect",
    # D10, resolved 2026-09-27: a CONFIRMED Bookings rendez-vous is no
    # longer editable with a warning — the connector changes only its
    # dossier and its notes, and refuses the rest, pointing to Outlook.
    "rendez-vous can be edited",
    "la modification reste dans athéna",
    "pour l'annuler, update_hearing",
    # Review of D10: « any other change — do it in Outlook » is false for a
    # type, a reminder, a court or a judge (not on the Outlook meeting) and
    # for a title or a location (an Outlook edit is never carried back —
    # the sync compares the slot only). Outlook takes the reschedule and the
    # cancellation; the rest is the application's.
    "en changer autre chose, faites-le dans outlook",
    "tell the user to make it in outlook",
    "make the change in outlook",
    # ... and the divergence warning named the client as the mover, while
    # D10 now sends the LAWYER to Outlook to reschedule.
    "le client a déplacé ou annulé ce rendez-vous",
    # Lot 2A (T7): update_document writes a document's name, date, tags,
    # folder and a presumed category; manage_folder writes the filing tree.
    # The « document » NEVER that said otherwise was narrowed to the FILE.
    "its name and its folder are read-only",
    "the one thing you can write is its analysis",
    "seule son analyse s'y inscrit",
    "son nom ou son dossier de classement",
    # Lot 2A (T8): fill_gabarit and create_document ADD a new document to a
    # dossier. The « document » NEVER that said otherwise was narrowed to an
    # EXISTING document's file.
    "never adds a new file to a dossier",
    "en verser un nouveau au dossier",
    # Lot 2A (T11, the text sweep). update_document sets a PRESUMED category
    # on an unanalysed document (D15), so « the category is never chosen »
    # holds for an ANALYSIS only; a replaced template file prints at once on
    # every document of its kind, so the active template DOES change without
    # the lawyer (only its DESIGNATION never does); and `query` never
    # matched a « description » field after 2026-08-31.
    "you cannot choose or invent one. the",
    "derived from the one you pick, never chosen",
    "change jamais sans vous",
    "filename, description, tags",
    "names, description and tags",
    # Review of T11. A template's NAME is scanned only when a template is
    # CREATED — a rename has no source dossier to be checked against. A
    # copy's category stays the LAWYER's when he set it, and its inherited
    # protection level has nothing to confirm (it is verified by qualifying
    # the copy). The upload link is not « valid one hour »: the GCS session
    # outlives the ticket — what lasts the hour is the FILING.
    "refused while it or the template's name",
    "lui ou le nom du gabarit nomme",
    "category and protection level, presumed",
    "niveau de protection de l'original, à confirmer",
    "lien de dépôt valable une heure",
    "le lien expire sans effet",
    # Lot 3b (BILL): the connector issues, promotes and voids invoices —
    # the promises « never changes an invoice's status » and « never
    # allocates an invoice number » were DELETED from the registry, and
    # each phrasing that stood on a surface is false now.
    "never changes an invoice's status",
    "ne change jamais le statut d'une facture",
    "cannot void an invoice",
    "ne peut pas annuler une facture",
    "cannot move one out of brouillon",
    "it never allocates an invoice number",
    "never sets an invoice status",
    "l'annulation dans l'application est la seule voie",
    # Lot 3b, the text step: the phrasings of the same falsified promises
    # that stood on the consent screen, in INSTRUCTIONS or in a schema
    # (review of lot 3) — the connector voids an invoice, promotes a
    # brouillon, and complete_task is one status change among several.
    "rien ne s'annule depuis le connecteur",
    "plus rien ici ne pourra les modifier",
    "is the only status change here",
    "never promoted here",
    "atterrit en brouillon et y reste",
    "lands in brouillon and stays there",
    # Review of the lot-3b text step: the RECLASSIFY paragraph still said
    # the reclassifiers « are the only tools that reach a row already
    # carried to an invoice » — the void (update_invoice, status annulée)
    # reaches every one of them, to release it.
    "only tools that reach a row already carried",
    "only writes that reach a row already carried",
    # Completeness review of lot 3: CORRECT said a move makes « both
    # dossiers' budget actuals change » — false for a NON-BILLABLE time
    # entry, which budget.aggregate_actuals never counts (the handler's own
    # move warning already said so; INSTRUCTIONS generalised past it).
    "both dossiers' budget actuals change",
    # Fixups of lot 3 (invoice numbers): « never reissued » is the YEAR
    # COUNTER's promise, not an absolute one — an imported number is
    # importable again once the voided invoice is deleted in the
    # application, and so is a past year's counter number (CLAUDE.md, the
    # D-2 residue). Said without its subject, it was false.
    "never reissued, even once voided",
    "il ne sera jamais réattribué, même si",
    # Fixups of lot 3 (D18, the lawyer's « refuse on a confirmed one »): a
    # category stored before the marker is the LAWYER'S unless it is
    # « autre » or blank. The lot-2A fixup texts said the opposite — that
    # such a category (changed on the form before the marker) stayed
    # replaceable, and that a pre-marker document reads « not chosen ».
    "faute d'historique, une catégorie que vous aviez",
    "a document older than the marker — reads",
    "ou document antérieur à ce suivi",
    # Lot 4b (DOSSIERS): set_dossier_status changes a dossier's status
    # through the same drain service the application's form uses — the
    # « dossier_status » promise was DELETED, and each phrasing of it that
    # stood on a surface is false now.
    "is set at creation and can never be changed here",
    "a davx5 drain only the application performs",
    "ce que seule l'application fait",
    "it can never be changed afterwards through this connector",
    "is deliberately not accepted: closing a dossier",
    "fixez le statut à la création",
    "ne se change pas par le connecteur",
    # Lot 4b (CONTACTS): record_kyc_status INSCRIBES a presumed compliance
    # check and update_partie_mandataire writes a contact's representations
    # — « trust_identity » was split, and these phrasings are false now.
    "never touches trust accounting, identity verification",
    "à la vérification d'identité ou à la vérification des conflits",
    "not writable here and never will be",
    "mandataires are not writable",
    "never verifies an identity or a conflict",
    # Review of lot 4b step 3: the phone never writes a dossier record
    # (DavX5 syncs its tasks, notes and events — separate documents),
    # so a phone edit can never be what refuses a dossier write.
    "au fidéicommis ou sur votre téléphone",
    # Lot 4b, the text step: the claude.ai skill's phrasings of the same
    # falsified promises (DEPLOYMENT.md §15 « Lot 4 » lists them for the
    # skill's own update) — none may ever enter a connector text.
    "ne peut plus jamais être changé ici",           # a dossier's status
    "status` jamais modifiable ensuite",
    "il ne ferme pas un dossier",
    "connecteur ne ferme pas un dossier",
    # « Rien ne peut être supprimé » stays true only WITH its precision:
    # a detached party or mandataire is a link. The two English forms of
    # the never that ended without it — before lot 1b, and before lot 4b —
    # are the claims lot 4b narrowed. Quoted WHOLE (review of lot 4b step
    # 4): « a cancelled … event is kept, with its status. » alone is TRUE,
    # and a false-claim entry must never refuse a true sentence.
    "nothing can ever be deleted here.",
    "deleted here: a cancelled task or event is kept, with its status.",
    # Fixes of lot 4: an incomplete set_dossier_status and a refresh_names
    # that refused a dossier promised « the same idempotency_key is fine »
    # WITHOUT exception. A release that fails on a store blip leaves the
    # claim pending, and the same-key retry is then refused « encore en
    # cours », then « interrompu »: the texts now name those two outcomes
    # and the way out of each (a re-read, then a NEW key).
    "the same idempotency_key is fine",
    "idempotency_key convient",
    # Lot 5, step 5 (the final « never » set): the claims its texts
    # replaced, and the claude.ai skill's phrasings of the promises lot 5
    # falsified (DEPLOYMENT.md §15 « Lot 5 » lists them for the skill's own
    # update). The reversal is the only correction of a TRUST entry (and of
    # an administration entry no longer editable), never of « a register
    # entry »; a trust recette or déboursé inscribes nothing at
    # administration (only a fee payment does); the administration revision
    # trail keeps its 25 latest changes, not « every » one.
    "le fidéicommis est en lecture seule intégrale",
    "`reverse_register_entry` is the only correction:",
    "the only correction of a register entry",
    "c'est la seule correction, et l'original",
    "paiements d'honoraires — chacun inscrit",
    "chaque correction étant conservée",
    "every change is kept in the entry's revision trail",
    # Finitions: set_dossier_status sets a closed dossier's closed_date
    # (lot 4b) — IMP-05 said only the creation could (truth-1); create_note
    # has an idempotency_key (contracts-7).
    "ne fixe la date de fermeture qu'à la création",
    # truth-6: a fee payment is recorded by the accounting tools too.
    "par un « paiement d'honoraires » inscrit dans l'application.",
    "there is no de-duplication and a retry creates a second note",
    # D25 (2026-09-29): an analysis KEEPS a category the lawyer chose or
    # confirmed — the texts that said it replaced the stored category
    # unconditionally, and the warning that called the replaced one his.
    "and replaces the document's stored category",
    "an analysis replaces it)",
    "la catégorie est remplacée, le niveau",
    "posée dans l'application, est remplacée",
    "the stored category is derived from it",
    # Review of D25: the FILES paragraph handed every analysed document's
    # category to record_document_analysis — which KEEPS the lawyer's.
    "`record_document_analysis`'s) — never over",
)
KNOWN_FALSE_PATTERNS: tuple[str, ...] = (
    # Review of T11: NO template may be designated (a fresh store, or before
    # scripts.designer_gabarits_actifs ran) — « at most one », never « one ».
    r"(?<!at most )\bone of each is active",
    r"\bsignée\b",                         # « signée Claude »
    r"ne peut plus les modifier(?!, sauf)",       # the phase stays reclassifiable
    r"nothing here can modify them afterwards(?! except)",
    # The same billing-freeze claim in its other phrasings: set_*_phase
    # reaches an invoiced row (and so does the application's phase form).
    r"ne modifie(?:nt)? (?:jamais )?une entrée facturée",
    r"définitivement immodifiables",
    r"nothing here can touch it",
    r"neither this connector nor the application can modify it",
    # Review of lot 4 (the design's honesty test): record_kyc_status
    # INSCRIBES a presumed check — « cannot … verify an identity » in any
    # phrasing is false now, as « cannot … file a signification » was.
    r"\bcannot\b[^.]{0,80}\bverify an identity",
    r"\bne peut\b[^.]{0,80}\bvérifier une identité",
    # The skill's other phrasings of the two promises lot 4b falsified,
    # bound to their SUBJECT (review of lot 4b step 4): as bare substrings
    # they refused true sentences — « les paiements ne sont inscriptibles
    # par aucun outil », « terminer une tâche ne ferme pas un dossier »,
    # « closing a dossier is done in the application or by
    # set_dossier_status ».
    r"\b(?:conflits|mandataires)\s+ne sont inscriptibles par aucun outil",
    r"\b(?:identity|conflict|mandataires?)\b[^.]{0,80}\bnot writable by any "
    r"tool",
    r"\bconnector\s+cannot close a dossier",
    r"closing a dossier is done (?:only )?in the application(?: only)?\s*"
    r"(?:[.;)]|$)",
    # Lot 5, step 5 (the final « never » set): under the accounting grant
    # the connector WRITES both registers and a payment exists as a register
    # entry. Bound to their subject or to the absence of the grant's
    # qualification, so the true sentences that name the grant still pass
    # (« Without the separate … grant this connector never records a
    # payment; under it … »).
    r"(?<!grant )\b(?:this|the) connector never records a payment",
    r"(?<!grant it )\bnever touches trust accounting",
    r"\bconnecteur\b[^.]{0,80}\bn'enregistre aucun paiement",
    r"\bpaiements?\s+ne sont inscriptibles par aucun outil",
    r"\btrust (?:accounting |register )?is (?:entirely |fully )?read-only",
)


def _connector_files() -> dict[str, pathlib.Path]:
    """Every connector module, SUBPACKAGES included — a never swept over
    ``mcp/*.py`` alone would stop seeing a handler moved one level down."""
    root = _ATHENA / "mcp"
    return {
        "mcp/" + p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts
    }


def _swept_files() -> dict[str, pathlib.Path]:
    return {
        rel: p for rel, p in _connector_files().items()
        if rel not in disclosure.SWEEP_EXCLUDED
    }


# ══════════════════════════════════════════════════════════════════════
# 1. The families
# ══════════════════════════════════════════════════════════════════════


def test_the_families_partition_the_write_tools():
    seen: dict[str, str] = {}
    for family in disclosure.FAMILIES:
        assert family.tools, f"{family.key}: a family that grants nothing"
        for name in family.tools:
            assert name not in seen, f"{name} in {seen.get(name)} and {family.key}"
            seen[name] = family.key
    assert set(seen) == tools.WRITE_TOOLS
    assert disclosure.write_tools() == tools.WRITE_TOOLS
    keys = [f.key for f in disclosure.FAMILIES]
    labels = [f.label for f in disclosure.FAMILIES]
    assert len(keys) == len(set(keys)) and len(labels) == len(set(labels))


def test_each_family_scope_is_its_members_declared_scope():
    for family in disclosure.FAMILIES:
        assert family.scope in (mcp.SCOPE_WRITE, mcp.SCOPE_COMPTABILITE), family.key
        for name in family.tools:
            assert tools.TOOLS[name]["scope"] == family.scope, (family.key, name)


def test_each_family_names_every_member_and_has_its_partial():
    for family in disclosure.FAMILIES:
        for name in family.tools:
            assert f"`{name}`" in family.instructions_en, (family.key, name)
        assert family.consent_template.startswith("mcp/families/_")
        assert (_TEMPLATES / family.consent_template).is_file(), family.consent_template
        assert family.checkbox_summary_fr.strip(), family.key
    # No orphan partial: a partial with no family would never render.
    partials = {f"mcp/families/{p.name}"
                for p in (_TEMPLATES / "mcp" / "families").glob("*.html")}
    assert partials == {f.consent_template for f in disclosure.FAMILIES}


def test_instructions_name_every_write_tool_and_derive_their_counts():
    """Two variants since lot 5b, each counting and naming exactly the tools
    its token can see: the base one (every token without the accounting
    grant) leaves the ACCOUNTING family and get_admin_ledger out — a model
    told of a tool it cannot list looks for it — while the variant served to
    a token holding athena:comptabilite counts and names them all."""
    text = endpoint.INSTRUCTIONS
    assert text == disclosure.build_instructions()
    visible_writes = tools.WRITE_TOOLS - tools.ACCOUNTING_TOOLS
    for name in visible_writes:
        assert f"`{name}`" in text, name
    for name in tools.ACCOUNTING_TOOLS:
        assert f"`{name}`" not in text, name
    families = [f for f in disclosure.FAMILIES
                if f.tools and f.scope != mcp.SCOPE_COMPTABILITE]
    reads = len(set(tools.TOOLS) - tools.WRITE_TOOLS - tools.ACCOUNTING_TOOLS)
    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS became a SAFETY CORE plus ONE index line per family, the family prose moved into the tool descriptions: the counts open the index, whose lines follow in family order
    # (the parenthesized label list they replace was a second copy of them).
    header = (f"TOOLS: {reads} tools read; {len(visible_writes)} write, in "
              f"{len(families)} families:")
    assert header in text
    positions = [text.index(f"{family.label}: ") for family in families]
    assert positions == sorted(positions)
    assert positions[0] > text.index(header)
    assert "ACCOUNTING: " not in text

    full = endpoint.INSTRUCTIONS_COMPTABILITE
    assert full == disclosure.build_instructions(accounting=True)
    for name in tools.WRITE_TOOLS | tools.ACCOUNTING_TOOLS:
        assert f"`{name}`" in full, name
    every = [f for f in disclosure.FAMILIES if f.tools]
    assert (
        f"TOOLS: {len(set(tools.TOOLS) - tools.WRITE_TOOLS)} tools read; "
        f"{len(tools.WRITE_TOOLS)} write, in {len(every)} families:"
    ) in full
    assert "ACCOUNTING: " in full


def test_no_count_is_typed_by_hand():
    """The counts went stale twice while they were literals. No connector
    string — docstrings included, they drift the same way — may state a
    tool or family count in words or digits."""
    number = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
    # « in N families » is INSTRUCTIONS' own phrasing — built by an
    # f-string, so its constant parts never hold the number; the domaine
    # taxonomy's « 20 families » counts no tool and stays legal.
    pattern = re.compile(
        rf"(?:\b{number} (?:tools read|read tools|write tools)\b"
        rf"|\bin {number} (?:write )?families\b)",
        re.IGNORECASE,
    )
    assert pattern.search("27 tools read; 22 write, in five families")
    assert not pattern.search("kind='domaines' (20 families)")
    for rel, path in _connector_files().items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not pattern.search(node.value), (rel, node.value[:80])


def test_the_instructions_follow_the_registry(monkeypatch):
    """Fed a registry, the builder reflects it: an accounting tool adds the
    separate-grant sentence, an etag-accepting tool is named, and the counts
    move with the registry."""
    registry = dict(tools.TOOLS)
    # REWRITTEN in lot 5b: this read « athena:comptabilite not in base » —
    # true while the scope was dormant. Two promises now name the scope in
    # every variant (payment, invoice_paid — each worded true for every
    # token); what still follows the registry is the SCOPE SENTENCE.
    base = disclosure.build_instructions(registry, frozenset(), 50)
    assert "Accounting tools appear only" not in base
    with_accounting = disclosure.build_instructions(registry, frozenset({"x"}), 50)
    assert "`athena:comptabilite`" in with_accounting
    assert "Accounting tools appear only" in with_accounting
    assert "never stands in" in with_accounting
    holding = disclosure.build_instructions(registry, frozenset({"x"}), 50,
                                            accounting=True)
    assert "which this authorization holds" in holding
    assert "Accounting tools appear only" not in holding
    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS became a SAFETY CORE plus ONE index line per family, the family prose moved into the tool descriptions: the hand-assembled list of every etag-accepting tool (~1 KB)
    # left — the SAFETY CORE states the rule once, and each such tool's own
    # schema declares the argument and names the reads that carry the etag.
    # REWRITTEN deliberately (2026-09-30, the context-cost lot): the core
    # was compressed to leave the CONVENTIONS room within the client's
    # 2 048-character cut — « Where a tool takes `expected_etag`, pass the
    # `etag` of your latest read » became the equation below, same rule.
    assert "`expected_etag` = your latest read's `etag`" in base
    for name in tools.TOOLS:
        if tools.TOOLS[name].get("concurrency") in ("optional", "required"):
            props = tools.TOOLS[name]["input_schema"]["properties"]
            assert "expected_etag" in props or "expected_etags" in props, name
    registry["zz_extra_read"] = {"input_schema": {}}
    grown = disclosure.build_instructions(registry, frozenset(), 50)
    assert f"{len(registry) - len(tools.WRITE_TOOLS)} tools read" in grown
    # The reclassifiers' ceiling comes from the registry, never a literal.
    assert "up to 7 rows a call" in disclosure.build_instructions(
        dict(tools.TOOLS), frozenset(), 7)


def test_the_instructions_carry_the_lot_0a_rules():
    text = endpoint.INSTRUCTIONS
    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS became a SAFETY CORE plus ONE index line per family, the family prose moved into the tool descriptions: the same rules, in the core's wording; the voided invoice's
    # number is said by import_invoice, where the undo is described.
    core = disclosure.safety_core_en()
    assert "`expected_etag`" in core and "wrote nothing" in core
    assert "ENREGISTRÉE — NE PAS RÉESSAYER" in core and "do NOT retry" in core
    assert "then the SAME key, never a new one" in core
    assert "`updated_via`" in text and "`mcp_updated_at`" in text
    assert "the number itself stays on the voided invoice" in (
        tools.TOOLS["import_invoice"]["description"])
    # REWRITTEN in lot 5b: the scope was dormant and never advertised. Now a
    # token without it is told only that accounting tools need a separate
    # grant — never what they do; the promises the grant keeps reach the
    # tokens holding it.
    assert tools.ACCOUNTING_TOOLS
    assert "Accounting tools appear only under the SEPARATE" in text
    for never in disclosure.accounting_nevers():
        assert never.en not in text, never.key
        assert never.en in endpoint.INSTRUCTIONS_COMPTABILITE, never.key


def test_the_checkbox_summary_names_every_write_family():
    summary = str(disclosure.write_summary_fr())
    for family in disclosure.families_for(mcp.SCOPE_WRITE):
        assert family.checkbox_summary_fr.lower() in summary.lower()
    assert summary[0].isupper()
    # The GENERAL promises only (lot 5b): what the accounting grant itself
    # never does is the accounting box's to say, beside what it grants.
    for never in disclosure.general_nevers():
        if never.summary_fr:
            assert f"jamais {never.summary_fr}" in summary.lower()
    for never in disclosure.accounting_nevers():
        if never.summary_fr:
            assert f"jamais {never.summary_fr}" not in summary.lower(), never.key


def test_the_accounting_checkbox_summary_names_its_family_and_its_nevers():
    """The accounting box's label is derived like the write box's: the
    ACCOUNTING family's clause, then the promises the grant keeps."""
    summary = str(disclosure.comptabilite_summary_fr())
    families = disclosure.families_for(mcp.SCOPE_COMPTABILITE)
    assert [f.key for f in families] == ["accounting"]
    for family in families:
        assert family.checkbox_summary_fr.lower() in summary.lower()
    assert summary[0].isupper()
    for never in disclosure.accounting_nevers():
        if never.summary_fr:
            assert f"jamais {never.summary_fr}" in summary.lower(), never.key


# ══════════════════════════════════════════════════════════════════════
# 2. The « never » sweep — over the syntax tree, never the text
# ══════════════════════════════════════════════════════════════════════


def _references(tree: ast.AST) -> set[str]:
    """Every identifier the module can REACH: a name or attribute load, an
    imported name (aliased or not), or ``getattr``/``hasattr`` with a
    constant. Definitions and string literals are not references."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.add(node.args[1].value)
    return found


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _calls_missing(tree: ast.AST, call: str, keyword: str) -> list[int]:
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == call
        and not any(k.arg == keyword for k in node.keywords)
    ]


def _calls_to(tree: ast.AST, call: str) -> int:
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == call
    )


def violations(
    files: dict[str, str], *, tool_code: "frozenset[str] | set[str]" = (
        disclosure.TOOL_CODE_MODULES),
) -> list[str]:
    """Every breach of every NEVER in *files* ({relative path: source}).

    *tool_code* — the modules the tool-code patterns (a bare ``.delete()``)
    also apply to: the registry's TOOL_CODE_MODULES, plus, for the real
    sweep, every service module a connector module reaches."""
    out: list[str] = []
    for rel, source in sorted(files.items()):
        if rel in disclosure.SWEEP_EXCLUDED:
            continue
        tree = ast.parse(source)
        refs = _references(tree)
        modules = _imported_modules(tree)
        for never in disclosure.NEVERS:
            patterns = list(never.forbidden)
            if rel in tool_code:
                patterns += list(never.forbidden_in_tool_code)
            for pattern in patterns:
                for ident in sorted(refs):
                    if re.fullmatch(pattern, ident):
                        out.append(f"{rel}: {never.key} forbids {ident}")
            for module in never.forbidden_modules:
                if module in modules:
                    out.append(f"{rel}: {never.key} forbids importing {module}")
            for call, keyword in never.required_keywords:
                for line in _calls_missing(tree, call, keyword):
                    out.append(
                        f"{rel}:{line}: {never.key} — {call}() without {keyword}="
                    )
    return out


def _sources() -> dict[str, str]:
    return {rel: p.read_text(encoding="utf-8") for rel, p in _connector_files().items()}


def _service_names(source: str) -> set[str]:
    """The ``services`` modules a source imports, in any form."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "services":
                names.update(alias.name for alias in node.names)
            elif node.module.startswith("services."):
                names.add(node.module.split(".", 2)[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("services."):
                    names.add(alias.name.split(".", 2)[1])
    return names


def services_reached(
    files: dict[str, str], read: "dict[str, str] | None" = None,
) -> dict[str, str]:
    """``{"services/X.py": source}`` of every service module the connector
    modules in *files* import — transitively (a service importing a
    service). *read* maps a service name to its source (tests); the disk
    otherwise.

    Since lot 1b (L6) the protocol tools write THROUGH a service
    (``services/protocoles.py``, the door the web routes use), so a sweep
    of the ``mcp`` package alone would no longer prove what a « never »
    promises: a service reached by a tool could call a forbidden model
    function — a ``delete_step`` in a step edit — with the sweep green."""
    out: dict[str, str] = {}
    pending = sorted({n for src in files.values() for n in _service_names(src)})
    while pending:
        name = pending.pop()
        rel = f"services/{name}.py"
        if rel in out:
            continue
        if read is not None:
            source = read.get(name)
        else:
            path = _ATHENA / "services" / f"{name}.py"
            source = path.read_text(encoding="utf-8") if path.exists() else None
        if source is None:
            continue
        out[rel] = source
        pending.extend(sorted(_service_names(source) - {
            r.split("/", 1)[1][:-3] for r in out}))
    return out


def test_no_connector_module_breaks_a_never():
    sources = _sources()
    reached = services_reached(sources)
    assert violations(
        {**sources, **reached},
        tool_code=disclosure.TOOL_CODE_MODULES | set(reached),
    ) == []


def test_the_sweep_follows_the_services_the_tools_reach():
    """Non-vacuous: the protocol service is swept, and a forbidden call
    planted in a service a connector module imports — in each import
    form — is reported, including a bare ``.delete()`` (tool code)."""
    assert "services/protocoles.py" in services_reached(_sources())
    planted = {"relay": "from services import deep as d\n",
               "deep": "note_model.delete_note(n)\nref.delete()\n"}
    for form in ("from services import relay as r\n",
                 "from services.relay import something\n",
                 "import services.relay\n"):
        files = {"mcp/probe.py": form}
        reached = services_reached(files, read=planted)
        assert set(reached) == {"services/relay.py", "services/deep.py"}
        found = violations({**files, **reached},
                           tool_code=set(reached))
        assert any(v.startswith("services/deep.py: delete forbids "
                                "delete_note") for v in found), found
        assert any(v == "services/deep.py: delete forbids delete"
                   for v in found), found


def test_the_sweep_reads_every_connector_module_but_the_registry():
    swept = set(_swept_files())
    assert {"mcp/handlers.py", "mcp/tools.py", "mcp/endpoint.py",
            "mcp/write_support.py"} <= swept
    assert "mcp/disclosure.py" not in swept
    assert disclosure.SWEEP_EXCLUDED == {"mcp/disclosure.py"}
    assert disclosure.TOOL_CODE_MODULES <= swept


def test_the_sweep_ignores_strings_and_sees_every_reference_form():
    """The detector itself: prose never counts, and no evasive form escapes."""
    prose = {"mcp/probe.py": (
        '"""record_payment(x) — void_invoice, delete_task."""\n'
        '# update_kyc_status(\n'
        'MESSAGE = "toggle_task_complete create_transaction"\n'
    )}
    assert violations(prose) == []
    evasive = {
        "mcp/probe_a.py": "from models.invoice import record_payment as rp\n",
        "mcp/probe_b.py": "import services.encaissements as enc\n",
        "mcp/probe_c.py": "from services import encaissements\n",
        "mcp/probe_d.py": "cb = task_model.toggle_task_complete\n",
        # Rewritten deliberately (lot 3b): void_invoice is no longer
        # forbidden (update_invoice voids), so the getattr form is probed on
        # a call that still is.
        "mcp/probe_e.py": "getattr(invoice_model, 'record_payment')(i)\n",
        "mcp/probe_f.py": "note_model.delete_note(n)\n",
        "mcp/probe_g.py": "invoice_model.create_invoice(d, [], [], {})\n",
        # Rewritten deliberately (lot 5b): models.admin_ledger is no longer
        # a forbidden import — the accounting tools reach the registers
        # through services/comptabilite — so the module-import form is
        # probed on a module that still is.
        "mcp/probe_h.py": "import utils.courriel\n",
    }
    found = violations(evasive)
    for rel in evasive:
        assert any(v.startswith(rel) for v in found), (rel, found)


def test_each_forbidden_pattern_is_caught_on_a_planted_call():
    """Non-vacuous per entry: a planted reference to an identifier each
    pattern matches is reported, under that entry's key."""
    for never in disclosure.NEVERS:
        for pattern in never.forbidden:
            ident = pattern.replace(r"\w+", "x")
            assert re.fullmatch(pattern, ident), pattern
            found = violations({"mcp/probe.py": f"model.{ident}(1)\n"})
            assert any(f"{never.key} forbids {ident}" in v for v in found), (pattern, found)
        for pattern in never.forbidden_in_tool_code:
            ident = pattern.replace(r"\w+", "x")
            planted = f"ref.{ident}()\n"
            assert any(never.key in v for v in violations({"mcp/handlers.py": planted}))
            # ... and only there: the protocol layer deletes its bookkeeping.
            assert violations({"mcp/store.py": planted}) == []


def test_the_required_keyword_sweep_has_something_to_check():
    """Vacuity check: the one create_invoice call the connector makes is
    found — and it names its number."""
    tree = ast.parse(_sources()["mcp/handlers.py"])
    for never in disclosure.NEVERS:
        for call, keyword in never.required_keywords:
            assert _calls_to(tree, call) >= 1, call
            assert _calls_missing(tree, call, keyword) == []


def test_the_forbidden_inputs_are_declared_by_no_write_tool():
    for never in disclosure.NEVERS:
        for tool, prop in never.forbidden_inputs:
            targets = tools.WRITE_TOOLS if tool == "*" else {tool}
            assert targets <= set(tools.TOOLS), (never.key, tool)
            for name in targets:
                props = tools.TOOLS[name]["input_schema"].get("properties", {})
                assert prop not in props, (never.key, name, prop)


def test_every_behavioural_test_exists():
    for never in disclosure.NEVERS:
        if not never.behavioural_test:
            continue
        path, _, name = never.behavioural_test.partition("::")
        tree = ast.parse((_ATHENA / path).read_text(encoding="utf-8"))
        defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert name in defs, never.behavioural_test


def test_every_never_is_backed_by_a_mechanism_and_speaks_both_languages():
    keys = [n.key for n in disclosure.NEVERS]
    assert len(keys) == len(set(keys))
    for never in disclosure.NEVERS:
        assert never.fr.strip() and never.en.strip(), never.key
        assert (
            never.forbidden or never.forbidden_modules
            or never.forbidden_in_tool_code or never.required_keywords
            or never.forbidden_inputs or never.behavioural_test
        ), f"{never.key}: a promise nothing enforces"
        # No trailing punctuation: the template adds « ; » / « . ».
        assert not never.fr.rstrip().endswith((";", ".", "&nbsp;;")), never.key
        # Every promise reaches the tokens it binds (lot 5b): a general one
        # every token, an accounting one the tokens holding the grant.
        assert never.en in endpoint.INSTRUCTIONS_COMPTABILITE, never.key
        if not never.accounting_only:
            assert never.en in endpoint.INSTRUCTIONS, never.key


def test_the_registry_is_pure():
    """It may import the scope constants and markupsafe — never a model or
    a service. (mcp.tools derives WRITE_TOOLS from it at import, so a model
    import here would pull Firestore into every tools import.)"""
    tree = ast.parse((_ATHENA / "mcp" / "disclosure.py").read_text(encoding="utf-8"))
    # EVERY import, the lazy ones inside functions included: a model
    # imported lazily in a builder would still run Firestore at the first
    # consent render. (The lazy `from mcp import tools` is the one allowed.)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"__future__", "dataclasses", "typing", "markupsafe", "mcp"}, imported


def test_the_registry_names_forbidden_calls_only_as_strings():
    """The raw-text sweep of test_invoice_detail counts any file containing
    the payment writer's name followed by a parenthesis; the registry must
    never carry that text — nor any other connector module."""
    needle = "record_payment" + "("
    for rel, source in _sources().items():
        assert needle not in source, rel


# ══════════════════════════════════════════════════════════════════════
# 3. No known false claim, on any text surface the connector emits
# ══════════════════════════════════════════════════════════════════════


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the docstring constants — prose for maintainers, never emitted
    to a client (and the registry's own docstring RECOUNTS the false claims
    it replaced, which is the point of it)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _connector_strings() -> list[tuple[str, str]]:
    """Every string constant a connector module can EMIT — implicit
    concatenation already merged by the parser, so a claim split across
    source lines is still one string here. Docstrings excluded."""
    out = []
    for rel, source in _sources().items():
        tree = ast.parse(source)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                out.append((rel, node.value))
    return out


def _false_claims_in(text: str) -> list[str]:
    lowered = text.lower()
    hits = [c for c in KNOWN_FALSE_CLAIMS if c in lowered]
    hits += [p for p in KNOWN_FALSE_PATTERNS if re.search(p, text, re.IGNORECASE)]
    return hits


def test_no_known_false_claim_in_the_connector_texts():
    offenders = [
        (rel, hits, value[:100]) for rel, value in _connector_strings()
        if (hits := _false_claims_in(value))
    ]
    assert offenders == [], offenders
    assert _false_claims_in(endpoint.INSTRUCTIONS) == []
    # Lot 5, step 5: the text a token holding athena:comptabilite reads was
    # never scanned — the variant that speaks of money.
    assert _false_claims_in(endpoint.INSTRUCTIONS_COMPTABILITE) == []
    for name, spec in tools.TOOLS.items():
        assert _false_claims_in(spec["description"]) == [], name


def test_the_false_claim_detector_is_not_vacuous():
    for claim in KNOWN_FALSE_CLAIMS:
        assert _false_claims_in(f"xx {claim.upper()} yy")
    assert _false_claims_in("horodatée et signée «&nbsp;Claude&nbsp;»")
    assert not _false_claims_in("aucune signification consignée")
    assert _false_claims_in("le connecteur ne peut plus les modifier. Pour")
    assert not _false_claims_in("le connecteur ne peut plus les modifier, sauf leur phase")
    assert _false_claims_in("nothing here can modify them afterwards, and")
    assert not _false_claims_in("nothing here can modify them afterwards except")
    assert _false_claims_in("Le connecteur ne modifie jamais une entrée facturée")
    assert _false_claims_in("ni l'application ne modifient une entrée facturée")
    assert not _false_claims_in("Le connecteur ne corrige jamais une entrée facturée")
    assert _false_claims_in("once invoiced nothing here can touch it.")
    assert _false_claims_in(
        "neither this connector nor the application can modify it, and")


def test_the_lot_2a_texts_say_what_files_and_templates_do():
    """Lot 2A (T11): the FILES family lost the two template writes to its own
    TEMPLATES family, and the texts say the rules a caller or the lawyer
    must know — the upload link is the ONE link (write-only, a documented
    exception), the sandbox needs egress to storage.googleapis.com, a
    filled gabarit always lands in « Projets », a presumed category is the
    lawyer's to confirm, and a replaced template file is kept."""
    text = endpoint.INSTRUCTIONS
    assert "FILES: " in text and "TEMPLATES: " in text
    assert text.index("FILES: ") < text.index("TEMPLATES: ")
    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS became a SAFETY CORE plus ONE index line per family, the family prose moved into the tool descriptions: the egress, the « Projets » rule and the presumed category are
    # pinned in the descriptions of the tools that carry them.
    desc = {name: spec["description"] for name, spec in tools.TOOLS.items()}
    assert re.search(r"storage\.googleapis\.com", desc["begin_upload"])
    assert "in its « Projets » folder" in desc["fill_gabarit"]
    assert "stored PRESUMED until the lawyer confirms it" in desc["update_document"]
    assert "stays PRESUMED" in text                       # the FILES line
    keys = {n.key: n for n in disclosure.NEVERS}
    # The documented exception, stated as the promise it bounds.
    link = keys["link"]
    assert "`begin_upload`'s `upload_url`" in link.en and "write-only" in link.en
    assert {"get_signed_url", "sign_blob_url", "generate_signed_url",
            "build_folder_zip_url"} <= set(link.forbidden)
    assert "restorable" in keys["template_version"].en
    assert {"document", "confirm", "active_template"} <= set(keys)
    # The ANALYSE paragraph and the tool keep the D15 nuance: the category
    # is never chosen THERE, and a presumed one set in FILES is replaced.
    # REWRITTEN deliberately (D25, 2026-09-29): they pinned « an analysis
    # replaces it » (ANALYSE) and « it replaces a PRESUMED category a FILES
    # tool set » (the tool). Both still hold for a PRESUMED category — and
    # both now say the lawyer's is KEPT, never replaced. The « only for a
    # document WITHOUT an analysis » half lives in the FILES paragraph.
    # Rewritten again (contracts-1 part 2): the ANALYSE and FILES index
    # lines keep the rule in a clause; its detail is the tools' own.
    analyse = next(f for f in disclosure.FAMILIES if f.key == "analyse")
    assert "never over the lawyer's" in analyse.instructions_en
    assert "on any OTHER document that carries an analysis" in (
        tools.TOOLS["update_document"]["description"])
    desc = tools.TOOLS["record_document_analysis"]["description"]
    # Rewritten deliberately (the two confirmations separated, 2026-09-29):
    # it pinned « that one is KEPT, with his confirmation ». The category is
    # kept; the ANALYSIS confirmation never covers a new run.
    assert "that one is KEPT, and a gap is flagged" in desc
    assert "with his confirmation" not in desc
    assert "his confirmation of an earlier analysis never covers a new one" in desc
    assert "cannot choose or invent one here" in desc
    assert "REPLACES a PRESUMED one (a FILES tool's" in desc
    assert "NEVER a category the lawyer chose or confirmed" in desc
    assert "a template is not one" in tools.TOOLS["get_document_text"]["description"]
    assert "a template is not a document" in text   # READ-CONTENT
    assert "It never hands out a link that reads or downloads a stored file" in text
    files = (_TEMPLATES / "mcp" / "families" / "_files.html").read_text(
        encoding="utf-8")
    flat_files = " ".join(files.split())
    assert re.search(r"storage\.googleapis\.com", flat_files)
    assert "<strong>toujours</strong> dans «&nbsp;Projets&nbsp;»" in flat_files
    templates = (_TEMPLATES / "mcp" / "families" / "_templates.html").read_text(
        encoding="utf-8")
    flat_templates = " ".join(templates.split())
    assert "Gérer vos gabarits" in flat_templates
    assert "rétablissable" in flat_templates
    assert "Gérer vos gabarits" not in flat_files


def test_complete_task_describes_the_refusal_it_now_makes():
    desc = tools.TOOLS["complete_task"]["description"]
    assert "never reopens a closed task" in desc
    assert "REFUSED" in desc
    assert "complete_task" in tools.EDIT_TOOLS


def test_the_lot_3b_texts_say_what_billing_does():
    """Lot 3b (the text step). Every surface a client model or the lawyer
    reads says the BILL rules the code enforces — and none of the phrases
    the lot made false survives (KNOWN_FALSE_CLAIMS above):

    * a created invoice CONSUMES the year's next number for ever;
    * only a BROUILLON is corrected — an issued invoice is voided, then a
      new one issued;
    * a void is REFUSED while a payment stands, and never gives the number
      back;
    * marking envoyée sends nothing, and « payée » is never set here;
    * a budget version is kept, unchanged, as the proof of what the client
      was told;
    * an import lands in brouillon, and update_invoice (not import_invoice)
      promotes it.
    """
    from mcp.output_schemas import OUTPUT_SCHEMAS

    billing = next(f for f in disclosure.FAMILIES if f.key == "billing")
    assert billing.tools == ("create_invoice", "update_invoice",
                             "create_budget_version")
    assert "preview_invoice" in billing.instructions_en   # a read, named
    assert "get_budget" in billing.instructions_en        # a read, named
    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS became a SAFETY CORE plus ONE index line per family, the family prose moved into the tool descriptions: the index line keeps the three facts a caller needs before
    # opening the tools; « REFUSED while a payment stands » and « the proof
    # of what the client was told » are pinned below, in the descriptions.
    for fragment in ("CONSUMES the year's next number", "ONLY a brouillon",
                     "sending NOTHING"):
        assert fragment in billing.instructions_en, fragment
    keys = {n.key for n in disclosure.NEVERS}
    assert {"payment", "invoice_paid", "invoice_send", "invoice_sources"} <= keys

    desc = {name: spec["description"] for name, spec in tools.TOOLS.items()}
    assert "consumed FOR EVER" in desc["create_invoice"]
    assert "a brouillon only" in desc["update_invoice"]
    assert "REFUSED while a payment stands" in desc["update_invoice"]
    assert "Never payée" in desc["update_invoice"]
    assert "MARKED sent" in desc["list_invoices"]
    assert "proof of what the client was told" in desc["create_budget_version"]
    # D20 (2026-09-29): the sending is the lawyer's to attest — the import's
    # text names update_invoice as HIS instrument, never « promote it »
    # (rewritten deliberately: this line pinned « promote it with
    # update_invoice »).
    assert "update_invoice to envoyée at his word" in desc["import_invoice"]
    assert "promote it" not in desc["import_invoice"]
    assert "set it only on his word" in desc["update_invoice"]
    # Moved with the prose (contracts-1 part 2): what « envoyée » backs, and
    # how an issued invoice is corrected.
    assert "a fee payment from trust relies on it, art." in desc["update_invoice"]
    assert "corrected by voiding it and issuing a new one" in (
        desc["update_invoice"])
    assert "frozen, their phase aside, until update_invoice voids it" in (
        desc["create_invoice"])
    import_status = OUTPUT_SCHEMAS["import_invoice"]["properties"]["entity"][
        "properties"]["status"]["description"]
    assert "update_invoice does" in import_status

    # Review of the text step: RECLASSIFY is no longer the only family that
    # writes to a billed row — the void releases it — so the paragraph says
    # what stays true: the reclassifiers alone change a row WHILE it stays
    # billed, and the void releases the row instead.
    reclassify = next(f for f in disclosure.FAMILIES if f.key == "reclassify")
    assert "WHILE it stays carried to an invoice" in reclassify.instructions_en
    assert "(a void releases it instead)" in reclassify.instructions_en
    assert "WHILE it stays carried to an invoice" in endpoint.INSTRUCTIONS

    # Completeness review of lot 3: the move (CORRECT, `dossier_id`) says
    # what the budget really does — a non-billable time entry counts in NO
    # budget (budget.aggregate_actuals skips it), exactly as the handler's
    # own move warning says (_move_warnings).
    # Moved into update_time_entry (contracts-1 part 2), whose `dossier_id`
    # is the move; its twin says the disbursement's share follows too.
    assert "non-billable time counts in no budget" in desc["update_time_entry"]
    assert "its share of the budget actuals with it" in desc["update_expense"]

    # Fixups of lot 3 — ONE truth about invoice numbers on every surface:
    # the year counter never reissues a number; an IMPORTED number can be
    # imported again once the voided invoice is deleted in the application.
    importing = next(f for f in disclosure.FAMILIES if f.key == "import")
    assert "NEVER allocates a number" in importing.instructions_en
    assert "the counter never reissues it" in desc["create_invoice"]
    assert "an IMPORTED number can be imported again" in desc["update_invoice"]
    assert "only then can it be imported again" in desc["import_invoice"]
    with mock.patch("google.cloud.firestore.Client"):
        import mcp.handlers as _handlers
    assert _handlers._NUMBER_IS_PERMANENT.startswith(
        "Le numéro {number} est consommé DÉFINITIVEMENT : la numérotation de "
        "l'année")
    # ... and an uncertain creation is said as such, never « no number
    # consumed »: re-read, then the SAME key.
    assert "« Issue INCERTAINE »" in desc["create_invoice"]
    assert "with the SAME key" in desc["create_invoice"]

    # Fixups of lot 3 (D18): the FILES texts say a category stored before
    # the marker is the lawyer's unless it is « autre ».
    assert "counts as his unless its category is « autre »" in (
        desc["update_document"])



def test_the_lot_4b_texts_say_what_the_dossier_tools_do():
    """Lot 4b (DOSSIERS). The family says the consequences the lawyer
    consents to — closing DRAINS the phone and leaves the prescription
    alerts; the repair of an incomplete drain is the SAME status again; a
    detached party is a LINK removed, never the contact, refused for the
    last client, a served party and a client with trust history — and the
    promise it falsified is gone, while « nothing can be deleted » names
    the detach it narrows."""
    dossiers = next(f for f in disclosure.FAMILIES if f.key == "dossiers")
    assert dossiers.tools == ("set_dossier_status", "update_dossier_party")
    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS became a SAFETY CORE plus ONE index line per family, the family prose moved into the tool descriptions: the index line keeps the drain and the LINK; the rest is
    # pinned where the caller reads each tool.
    for fragment in ("DRAINS its DavX5 collection", "the contact stays"):
        assert fragment in dossiers.instructions_en, fragment
    status_desc = tools.TOOLS["set_dossier_status"]["description"]
    party_desc = tools.TOOLS["update_dossier_party"]["description"]
    for fragment in ("prescription alerts", "SAME status"):
        assert fragment in status_desc, fragment
    for fragment in ("EVER had trust funds", "a party a signification names",
                     "To ADD a party: update_dossier"):
        assert fragment in party_desc, fragment
    keys = {n.key: n for n in disclosure.NEVERS}
    assert "dossier_status" not in keys
    assert ("detaching a party or a mandataire removes a LINK — the contact "
            "stays") in keys["delete"].en
    assert "le contact reste" in keys["delete"].fr
    text = endpoint.INSTRUCTIONS
    assert "DOSSIERS: " in text
    assert "can never be changed here" not in text
    partial = (_TEMPLATES / "mcp" / "families" / "_dossiers.html").read_text(
        encoding="utf-8")
    flat = " ".join(partial.split())
    assert "jamais le contact" in flat
    assert "pour le dernier client, pour une partie signifiée" in flat
    desc = {name: spec["description"] for name, spec in tools.TOOLS.items()}
    assert "set_dossier_status" in desc["update_dossier"]
    assert "update_dossier_party" in desc["update_dossier"]
    assert "set_dossier_status" in (
        tools.TOOLS["create_dossier"]["input_schema"]["properties"]["status"]
        ["description"])



def test_the_lot_4b_texts_say_what_the_contact_tools_do():
    """Lot 4b (CONTACTS). A compliance check the connector inscribes is
    PRESUMED — « à confirmer » on the fiche, still OPEN in the coverage
    report, confirmed only by the lawyer — and never written over one he
    decided or confirmed; a detached mandataire is a LINK. The trust
    promise stays whole on its own, and the compliance one is backed by
    code: no connector call confirms, every update_kyc_status names its
    source, and no write tool takes the stored compliance fields."""
    contacts = next(f for f in disclosure.FAMILIES if f.key == "contacts")
    assert contacts.tools == ("update_partie_mandataire", "record_kyc_status")
    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS became a SAFETY CORE plus ONE index line per family, the family prose moved into the tool descriptions: the index line keeps PRESUMED and « NOT done »; the rest is
    # pinned in the two descriptions.
    for fragment in ("PRESUMED", "counts as NOT done"):
        assert fragment in contacts.instructions_en, fragment
    kyc_desc = tools.TOOLS["record_kyc_status"]["description"]
    for fragment in ("reported OPEN by get_coverage_report",
                     "this connector never confirms one",
                     "REFUSED on a check the lawyer decided or confirmed",
                     "`notes` are APPENDED under a dated"):
        assert fragment in kyc_desc, fragment
    assert "the mandataire contact stays" in (
        tools.TOOLS["update_partie_mandataire"]["description"])
    keys = {n.key: n for n in disclosure.NEVERS}
    assert "trust_identity" not in keys
    # Lot 5b narrowed it to the tokens WITHOUT the accounting grant — and
    # says so, so it stays true for every token. REWRITTEN in lot 5, step 5:
    # « touches » became « writes to » — reading the trust register stays
    # under athena:read, so the promise is about WRITES.
    assert keys["trust"].en == (
        "Without the separate `athena:comptabilite` grant it never writes "
        "to trust accounting — neither the trust register nor the "
        "administration ledger.")
    kyc_never = keys["kyc"]
    assert "confirm_kyc_status" in kyc_never.forbidden
    assert ("update_kyc_status", "source") in kyc_never.required_keywords
    assert ("*", "identity_verified_source") in kyc_never.forbidden_inputs
    # The SAFETY CORE states it as a clause (contracts-1 part 2).
    assert kyc_never.in_core
    assert ("CONFIRM an identity or conflict check, or change one the lawyer "
            "decided or confirmed") == kyc_never.en
    assert "à confirmer" in kyc_never.fr
    text = endpoint.INSTRUCTIONS
    assert "CONTACTS: " in text
    assert kyc_never.en in disclosure.safety_core_en()
    partial = (_TEMPLATES / "mcp" / "families" / "_contacts.html").read_text(
        encoding="utf-8")
    flat = " ".join(partial.split())
    assert "<strong>présumée</strong>" in flat
    assert "<strong>jamais</strong> modifiée par le connecteur" in flat
    desc = {name: spec["description"] for name, spec in tools.TOOLS.items()}
    assert "record_kyc_status" in desc["create_partie"]
    assert "update_partie_mandataire" in desc["update_partie"]
    assert "`*_presumed`" in desc["get_partie"]


def test_the_lot_4b_text_step_says_what_the_phone_and_the_record_keep():
    """Lot 4b, the text step (step 4). What the step-3 texts left unsaid,
    each pinned where it is TRUE — every assertion fails on the texts of
    4c802f9:

    * an incomplete drain is REPORTED and repaired by the same status again
      (or the application's « Resynchroniser le téléphone ») — and between
      actif and en_attente nothing changes on the phone;
    * a presumed compliance check counts as NOT done until the lawyer
      confirms it, and its notes carry Claude's dated line;
    * setting a dossier's status compare-and-sets like the other writes
      that rewrite what they read (it takes no etag);
    * list_deletions says it lists the LINKS detached, and its row schema
      says what each column names on a link row.
    """
    from mcp.output_schemas import OUTPUT_SCHEMAS

    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS became a SAFETY CORE plus ONE index line per family, the family prose moved into the tool descriptions: these two DOSSIERS facts are set_dossier_status's own.
    status_desc = tools.TOOLS["set_dossier_status"]["description"]
    assert ("between actif and en_attente nothing changes on the phone"
            in status_desc)
    # Reworded by the finitions (contracts-8): the moved-status case is one
    # of the three retry cases, no longer an « unless » tacked on.
    assert "`warnings` say the status moved during the call" in status_desc
    contacts = next(f for f in disclosure.FAMILIES if f.key == "contacts")
    assert "it counts as NOT done" in contacts.instructions_en

    text = endpoint.INSTRUCTIONS
    # The writes that rewrite what they read: ONE rule in the core now —
    # a stale refusal with `expected_etag` omitted, when the record changed
    # during the call — instead of a list of them.
    # REWRITTEN deliberately (2026-09-30, the context-cost lot): same rule,
    # compressed with the rest of the core to leave the CONVENTIONS room
    # within the client's 2 048-character cut.
    assert ("stale_etag (also without `expected_etag`, if the record changed "
            "during the call) wrote nothing") in disclosure.safety_core_en()
    assert "a dossier it refuses retried as its warning says" in (
        tools.TOOLS["update_dossier_party"]["description"])
    assert "`*_source` \"mcp\"" in text
    assert "« [AAAA-MM-JJ — inscrit par Claude] »" in text

    flat_dossiers = " ".join((_TEMPLATES / "mcp" / "families" / "_dossiers.html")
                             .read_text(encoding="utf-8").split())
    assert "le connecteur le <strong>signale</strong>" in flat_dossiers
    assert "un nouvel appel au même statut le reprend" in flat_dossiers
    assert "«&nbsp;Resynchroniser le téléphone&nbsp;»" in flat_dossiers
    assert ("<strong>en attente</strong> ne change rien au téléphone ni aux "
            "alertes") in flat_dossiers
    flat_contacts = " ".join((_TEMPLATES / "mcp" / "families" / "_contacts.html")
                             .read_text(encoding="utf-8").split())
    assert "ne compte <strong>pas comme faite</strong>" in flat_contacts

    deletions = tools.TOOLS["list_deletions"]["description"]
    assert "each LINK detached" in deletions
    assert "the contact stays" in deletions
    row = OUTPUT_SCHEMAS["list_deletions"]["properties"]["items"]["items"][
        "properties"]
    assert "a LINK detached" in row["entity_type"]["description"]
    assert "REPRESENTED" in row["title"]["description"]
    assert "the side it left" in row["status"]["description"]

    # The two patterns the text step added are live, and spare the truth.
    assert _false_claims_in(
        "this connector cannot create a protocol, verify an identity or file")
    assert _false_claims_in("le connecteur ne peut pas vérifier une identité")
    assert not _false_claims_in(
        "It never CONFIRMS an identity or conflict-of-interest check")


def test_review_of_the_lot_4b_text_step():
    """Review of lot 4b, step 4 — each assertion fails on 7f78871.

    * set_dossier_status's OWN description told a caller, without
      exception, to repair an incomplete drain by resending the SAME status
      — while its handler says the opposite when another writer moved the
      status during the call (resending would overwrite that newer status).
      The exception is now where the caller reads the tool, as it already
      was in INSTRUCTIONS.
    * Four false-claim entries refused TRUE sentences — a detector that
      cries wolf is disabled by the next person it blocks. Each is bound to
      its subject now, and still catches the claim it was written for.
    """
    desc = tools.TOOLS["set_dossier_status"]["description"]
    # Reworded by the finitions (contracts-8): three cases, stated once, in
    # order — the duplicated « unless » clause prescribed the very action it
    # was meant to be the exception to.
    assert "`warnings` say the status moved" in desc
    assert "never resend yours" in desc
    assert desc.count("unless") <= 1, desc
    # Fixes of lot 4: the same-key retry is no longer promised without
    # exception — the description names both refusals a retry can meet and
    # the way out of each; INSTRUCTIONS too.
    # REWRITTEN deliberately (contracts-1 part 2): the DOSSIERS paragraph
    # became an index line — the three cases are the description's, and
    # the core states the generic ones for every write.
    assert "normally" in desc and "still in flight" in desc
    assert "interrupted" in desc and "a NEW key" in desc
    core = disclosure.safety_core_en()
    assert "still in flight" in core and "a NEW key only if" in core

    # REWRITTEN in lot 5, step 5: « Les paiements ne sont inscriptibles par
    # aucun outil » was TRUE when lot 4b wrote this test, and lot 5 made it
    # false — under the accounting grant an encaissement or a fee payment
    # records one. It moved to the false list below.
    for true in (
        "A cancelled task or event is kept, with its status.",
        "Terminer la dernière tâche ne ferme pas un dossier.",
        "complete_task never closes anything else: it cannot close a "
        "dossier.",
        "Closing a dossier is done in the application or by "
        "set_dossier_status.",
        "Trust account numbers are not writable by any tool.",
    ):
        assert not _false_claims_in(true), true
    for false in (
        "NOTHING in Athéna can EVER be DELETED here: a cancelled task or "
        "event is kept, with its status.",
        "La vérification d'identité et la vérification de conflits ne sont "
        "inscriptibles par aucun outil",
        "Vérification d'identité, vérification de conflits et mandataires "
        "ne sont inscriptibles par aucun outil",
        "Il ne ferme pas un dossier",
        "This connector cannot close a dossier.",
        "Closing a dossier is done in the application only.",
        "Identity and conflict checks are not writable by any tool.",
        "Les paiements ne sont inscriptibles par aucun outil.",
    ):
        assert _false_claims_in(false), false


def test_the_lot_5_texts_state_the_final_never_set():
    """Lot 5, step 5 — the final « never » set. Each assertion fails on the
    texts of 17fb0d1:

    * « toucher au fidéicommis » is gone: the general promise is about
      WRITES (reading stays under athena:read), and beside the accounting
      box it points to that box's own list instead of a bare « sauf avec la
      case » that named nothing;
    * a payment exists ONLY as a register entry, and the payment bullet
      names both — the administration encaissement, the trust fee payment;
    * what stays impossible under the grant is the PRECISE list — no entry
      deleted (each register's correction said), no reconciliation, no
      account, no transfer between dossiers, no cash withdrawal, no fee
      payment on a paper, UNSENT or provision-imputing invoice, no bank
      number — each item its own promise, with its own sweep or test;
    * « the reversal is the only correction » is said of TRUST (and of an
      administration entry no longer editable) — never of every entry,
      beside update_admin_entry, which corrects one;
    * a trust recette or déboursé inscribes nothing at administration: the
      box's summary says it of the fee payment alone.
    """
    keys = {n.key: n for n in disclosure.NEVERS}

    trust = keys["trust"]
    for text in (trust.fr, trust.fr_comptabilite, trust.en):
        assert "toucher" not in text and "touches" not in text
    assert trust.fr == (
        "écrire au <strong>fidéicommis</strong> ou au registre "
        "d'administration")
    assert "énuméré avec elle, plus bas" in trust.fr_comptabilite

    payment = keys["payment"]
    for fragment in ("autrement que par une écriture aux registres comptables",
                     "un encaissement au compte d'administration",
                     "un paiement d'honoraires au fidéicommis",
                     "inscrit lui-même le paiement sur la facture"):
        assert fragment in payment.fr_comptabilite, fragment
    # REWRITTEN deliberately (contracts-1 part 2): the promise is a clause
    # of the SAFETY CORE now; the two register entries that record a
    # payment are named by the tools that make them.
    assert payment.in_core
    assert payment.en == "record a payment outside the accounting registers"
    for name in ("record_trust_entry", "record_admin_entry"):
        assert "RECORDS A PAYMENT" in tools.TOOLS[name]["description"], name

    # « fee_payee » joined on the lawyer's decision D23 (2026-09-29) — the
    # list is rewritten deliberately, the order kept.
    assert [n.key for n in disclosure.accounting_nevers()] == [
        "register_delete", "register_setup", "register_transfer",
        "trust_withdrawal", "fee_invoice", "fee_payee", "account_number"]
    listed_fr = " ".join(n.fr for n in disclosure.accounting_nevers())
    listed_en = " ".join(n.en for n in disclosure.accounting_nevers())
    for fr, en in (
        ("<strong>supprimer</strong> une écriture", "never deletes a register entry"),
        ("conciliation</strong>", "reconciliation"),
        ("un <strong>compte</strong>", "creates or modifies an account"),
        ("<strong>virer des fonds</strong> du fidéicommis d'un dossier à un autre",
         "never transfers trust funds between dossiers"),
        ("<strong>en espèces</strong> (art. 57", "in cash (art. 57"),
        ("facture papier", "with a paper invoice"),
        ("facture <strong>pas encore envoyée</strong>", "with an invoice not yet sent"),
        ("une <strong>provision</strong>", "an invoice that imputes a provision"),
        # D21 and D23 (2026-09-29).
        ("la facture d'un <strong>autre client</strong>",
         "another client's invoice than the one whose funds leave trust"),
        ("ni sur celle qui n'en nomme aucun, dans un dossier qui en compte "
         "plusieurs", "nor one naming no client, in a dossier of several"),
        ("à l'ordre de quelqu'un d'autre que <strong>vous ou votre cabinet</strong>",
         "to anyone but the lawyer or his firm"),
        ("<strong>numéro de compte</strong>", "a bank transit or account number"),
    ):
        assert fr in listed_fr, fr
        assert en in listed_en, en

    delete = keys["register_delete"]
    assert "au fidéicommis, une erreur ne se corrige que par une" in delete.fr
    assert "une écriture se corrige tant qu'elle reste modifiable" in delete.fr
    assert "a trust entry is corrected only by a reversal" in delete.en
    assert "`update_admin_entry` while editable" in delete.en
    # Each item carries the calls it forbids — split three ways.
    assert set(delete.forbidden) == {"delete_transaction", "delete_card_payment"}
    assert keys["register_transfer"].forbidden == ("create_inter_dossier_transfer",)
    assert "create_inter_dossier_transfer" not in keys["register_setup"].forbidden
    assert keys["register_transfer"].behavioural_test.endswith(
        "test_a_transfer_between_dossiers_is_never_reversed_here")
    assert keys["trust_withdrawal"].forbidden_inputs == (("*", "cash_receipt_id"),)
    assert keys["fee_invoice"].forbidden_inputs == (("*", "invoice_external_ref"),)
    # The behavioural test the fee-invoice promise names refuses all five
    # invoices — the paper one, the provision, the UNSENT draft and (D21)
    # another client's, or one naming no client in a dossier of several.
    source = (_ATHENA / "tests" / "test_mcp_accounting.py").read_text(encoding="utf-8")
    body = source[source.index(
        "def " + keys["fee_invoice"].behavioural_test.partition("::")[2]):]
    body = body[:body.index("\ndef ")]
    assert 'invoice_external_ref' in body and '"provision" in str(provision)' in body
    assert '"envoyée" in str(draft)' in body
    assert 'facture_autre_client' in body and 'facture_sans_client' in body
    assert keys["fee_payee"].behavioural_test.endswith(
        "test_d23_the_payee_is_the_lawyer_or_his_firm")

    summary = " ".join(str(disclosure.comptabilite_summary_fr()).split())
    assert summary.endswith(
        "Jamais de suppression, jamais de conciliation ni de compte, jamais "
        "de virement entre dossiers, jamais de retrait en espèces, jamais de "
        "paiement d'honoraires sur une facture papier, non envoyée, qui "
        "impute une provision ou adressée à un autre client, jamais de "
        "paiement d'honoraires à l'ordre d'un autre que vous ou votre "
        "cabinet.")

    accounting = next(f for f in disclosure.FAMILIES if f.key == "accounting")
    assert ("des recettes et des déboursés, et des paiements d'honoraires — "
            "dont chacun inscrit") in accounting.checkbox_summary_fr
    # The ACCOUNTING paragraph became an index line (contracts-1 part 2):
    # « the ONLY correction » is the tool's own sentence, pinned here.
    assert "`reverse_register_entry`" in accounting.instructions_en
    assert tools.TOOLS["reverse_register_entry"]["description"].startswith(
        "ACCOUNTING — WRITE. The ONLY correction of a trust entry, and of an "
        "administration entry update_admin_entry can no longer edit")
    assert "(its latest 25 kept)" in tools.TOOLS["update_admin_entry"]["description"]

    # The accounting promises reach the tokens holding the grant — and only
    # them; the general ones reach every token.
    for never in disclosure.accounting_nevers():
        assert never.en in endpoint.INSTRUCTIONS_COMPTABILITE, never.key
        assert never.en not in endpoint.INSTRUCTIONS, never.key
    assert trust.en in endpoint.INSTRUCTIONS
    assert payment.en in endpoint.INSTRUCTIONS


def test_the_lot_5_false_claim_entries_bite_and_spare_the_true_sentences():
    """Non-vacuous both ways: each lot 5 entry catches the phrasing it was
    written for, and the true sentences that NAME the grant still pass."""
    for false in (
        "Le fidéicommis est en lecture seule intégrale. Aucun outil n'y écrit.",
        "Le connecteur ne change aucun statut de facture et n'enregistre "
        "aucun paiement.",
        "This connector never records a payment.",
        "It never touches trust accounting.",
        "Trust accounting is read-only here.",
        "`reverse_register_entry` is the only correction: a fee payment…",
    ):
        assert _false_claims_in(false), false
    for true in (
        endpoint.INSTRUCTIONS, endpoint.INSTRUCTIONS_COMPTABILITE,
        "Without the separate `athena:comptabilite` grant this connector "
        "never records a payment; under it, a payment exists only as a "
        "register entry.",
        "Without the separate `athena:comptabilite` grant it never touches "
        "trust accounting.",
        "get_trust_balance is read-only.",
        "cet outil n'inscrit aucun paiement",
    ):
        assert not _false_claims_in(true), true
