"""Le connecteur facture — lot 3b (plan, lot 3 ; décisions D3, D9).

Cinq outils : ``preview_invoice`` et ``get_budget`` (lectures),
``create_invoice``, ``update_invoice`` et ``create_budget_version``
(famille BILL) ; ``update_time_entry`` / ``update_expense`` gagnent le
DÉPLACEMENT (``dossier_id``) et ``create_document`` la note d'honoraires
(``source: invoice_note``).

Tout passe par les VRAIS gestionnaires et les VRAIS modèles au-dessus du
faux Firestore partagé (``tests/_fake_firestore`` : le client, ses
transactions et la boucle de reprise sont les vrais) — on relit ce qui est
STOCKÉ, jamais un dict remis à un simulacre. Six familles :

1. **L'aperçu est le calcul de l'écriture** — même sélection, même document,
   même ``plan_invoice`` sous les drapeaux de ``create_invoice`` : sur une
   matrice de sources périmée, étrangère, non facturable, absente, de client
   manquant et de numéro de taxe vide, « prête » ⟺ la création réussit, et
   le total de l'aperçu est celui que la facture porte. L'aperçu n'écrit
   RIEN.
2. **Un numéro ne se brûle jamais** — une création refusée laisse le
   compteur annuel intact ; la même clé rejouée rend la PREMIÈRE facture, et
   le compteur n'avance qu'une fois.
3. **La facture se tient** — brouillon → envoyée (l'etag écrit rendu), le
   même statut deux fois n'écrit rien (avant l'etag), « en retard » seulement
   après l'échéance, jamais « payée » ; l'annulation exige un motif, est
   refusée tant qu'un paiement tient, libère les sources et ne rend pas le
   numéro ; la correction d'un brouillon, une modification par appel.
4. **Le budget s'ajoute** — v1 sur base 0, une base dépassée refusée sans
   écriture, une version identique n'écrit rien, merge/replace et leur diff.
5. **Le déplacement** — la règle unique de ``models/billing_move``.
6. **La note d'honoraires** — la génération du service, rendue quand elle
   est identique, le gabarit actif nommé.

Plus les deux tests comportementaux que deux promesses du registre
(``mcp/disclosure.NEVERS``) nomment, et les conformités d'``outputSchema``
de chaque branche.
"""

import ast
import inspect
import io
import itertools
import json
import os
import pathlib
import sys
import zipfile
from datetime import date, datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import mcp.tools as tools
    import services.note_honoraires as nh
    from models import doc_template as tpl_model
    from models import invoice as invoice_model
    from mcp.output_schemas import OUTPUT_SCHEMAS

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tests.test_mcp_output_schemas import _conforms  # noqa: E402
from utils import phases, storage_identity  # noqa: E402

UTC = timezone.utc
TODAY = date(2026, 9, 28)
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
COUNTER = "counters/invoices-2026"
TAXES = {"gst_number": "123456789 RT0001", "qst_number": "1234567890 TQ0001"}


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync",
                                                 "mcp.write_support"))
            and getattr(m, "db", None) is not None]


def _entry(eid: str, dossier: str = "d1", **over) -> dict:
    doc = {
        "id": eid, "dossier_id": dossier, "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "date": WHEN,
        "description": "Rédaction", "hours": 1.5, "rate": 30000,
        "amount": 45000, "billable": True, "invoiced": False,
        "invoice_id": None, "phase": "", "sous_phase": "",
        "created_at": WHEN, "updated_at": WHEN, "etag": f"{eid}-e0",
    }
    doc.update(over)
    return doc


def _expense(xid: str, dossier: str = "d1", **over) -> dict:
    doc = {
        "id": xid, "dossier_id": dossier, "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "date": WHEN,
        "description": "Timbre judiciaire", "category": "timbre_judiciaire",
        "amount": 5000, "taxable": True, "invoiced": False,
        "invoice_id": None, "phase": "", "sous_phase": "",
        "created_at": WHEN, "updated_at": WHEN, "etag": f"{xid}-e0",
    }
    doc.update(over)
    return doc


def _dossier(did: str, **over) -> dict:
    doc = {
        "id": did, "file_number": f"2026-{did}", "title": f"Dossier {did}",
        "status": "actif", "hourly_rate": 30000,
        "clients": [{"id": "p1", "name": "Jean Tremblay", "roles": [],
                     "avocat_id": "", "avocat_name": ""}],
        "client_ids": ["p1"], "opposing_parties": [], "opposing_party_ids": [],
        "created_at": WHEN, "updated_at": WHEN, "etag": f"{did}-e0",
    }
    doc.update(over)
    return doc


@pytest.fixture
def world(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(invoice_model, "today_mtl", lambda: TODAY)
    monkeypatch.setattr(handlers, "_today_mtl", lambda: TODAY)
    monkeypatch.setattr(handlers, "cabinet_dict", lambda: dict(TAXES))
    monkeypatch.setattr(nh, "cabinet_dict", lambda: {})
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: UID)
    fake.seed("parties/p1", {
        "id": "p1", "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay",
        "address_street": "1 rue A", "address_city": "Montréal",
        "address_province": "Québec", "address_postal_code": "H1A 1A1",
        "etag": "p1-e0"})
    fake.seed("dossiers/d1", _dossier("d1", file_number="2026-001",
                                      title="Tremblay c. Lavoie"))
    fake.seed("dossiers/d2", _dossier("d2", hourly_rate=25000))
    fake.seed("dossiers/d3", _dossier("d3", clients=[], client_ids=[]))
    fake.seed("timeentries/e1", _entry("e1"))
    fake.seed("timeentries/e2", _entry("e2", hours=0.5, amount=15000,
                                       description="Appel"))
    fake.seed("expenses/x1", _expense("x1"))
    fake.reset_logs()
    return fake


def _preview(**args):
    return handlers.preview_invoice({"dossier_id": "d1", **args})


_KEYS = itertools.count(1)


def _create(**args):
    """create_invoice with a key of its own — unless the test names one
    (the replay test does)."""
    base = {"dossier_id": "d1",
            "idempotency_key": f"cle-facture-{next(_KEYS):04d}"}
    return handlers.create_invoice({**base, **args})


def _issued(world, **args) -> dict:
    """An invoice created through the tool — its stored document."""
    selection = {"time_entry_ids": ["e1"], "expense_ids": ["x1"], **args}
    preview = _preview(**selection)
    assert preview["ready"], preview["refusals"]
    payload = _create(expected_total_cents=preview["total_cents"], **selection)
    return world.peek(f"invoices/{payload['entity']['id']}")


def _etag_of(world, invoice_id: str) -> str:
    return handlers.get_invoice({"invoice_id": invoice_id})["invoice"]["etag"]


# ══════════════════════════════════════════════════════════════════════
# 0. Le registre — ce que les textes promettent, les modèles le tiennent
# ══════════════════════════════════════════════════════════════════════


def test_the_hand_copied_ceilings_are_the_models_own():
    assert tools.INVOICE_NOTES_MAX_CHARS == invoice_model.DRAFT_NOTES_MAX_LENGTH
    assert (tools.INVOICE_TERMS_MAX_CHARS
            == invoice_model.DRAFT_PAYMENT_TERMS_MAX_LENGTH)
    assert tools.VOID_REASON_MAX_CHARS == invoice_model.VOID_REASON_MAX_LENGTH


def test_the_budget_enum_is_derived_and_never_carries_adm_nor_hor():
    enum = (tools.TOOLS["create_budget_version"]["input_schema"]["properties"]
            ["lines"]["items"]["properties"]["sous_phase"]["enum"])
    assert enum == sorted(
        c for c in phases.SOUS_CODES
        if phases.phase_of(c) not in phases.PHASES_NON_FACTURABLES)
    assert not {phases.phase_of(c) for c in enum} & {"ADM", "HOR"}


def test_the_status_enum_is_three_targets_the_model_knows():
    enum = tools.TOOLS["update_invoice"]["input_schema"]["properties"][
        "status"]["enum"]
    assert set(enum) <= set(invoice_model.VALID_STATUSES)
    assert not set(enum) & {"payée", "brouillon"}
    targets = {t for ts in invoice_model.STATUS_TRANSITIONS.values() for t in ts}
    assert set(enum) <= targets


def test_update_invoice_can_never_mark_an_invoice_paid(world):
    """The « invoice_paid » promise (mcp/disclosure): the schema refuses
    « payée », and a call that bypassed the schema is refused by the handler
    — nothing written, whatever the invoice's state."""
    schema = tools.TOOLS["update_invoice"]["input_schema"]
    assert tools.validate_args(
        schema, {"invoice_id": "i", "expected_etag": "", "status": "payée"})
    invoice = _issued(world)
    iid = invoice["id"]
    for state in ("brouillon", "envoyée", "en_retard"):
        world.external_write(f"invoices/{iid}", {**world.peek(f"invoices/{iid}"),
                                                 "status": state})
        before = world.peek(f"invoices/{iid}")
        world.reset_logs()
        with pytest.raises(tools.ToolArgumentError):
            handlers.update_invoice({"invoice_id": iid, "status": "payée",
                                     "expected_etag": before["etag"]})
        assert world.peek(f"invoices/{iid}") == before
        assert world.commits == []


def test_every_connector_invoice_creation_requires_all_sources():
    """The « invoice_sources » promise: EVERY call to the model's
    create_invoice in the connector passes require_all_sources=True — a
    literal True, never a variable a branch could flip."""
    tree = ast.parse(pathlib.Path(handlers.__file__).read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_invoice"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "invoice_model"
    ]
    assert len(calls) >= 2  # import_invoice and create_invoice
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert isinstance(kw.get("require_all_sources"), ast.Constant), call.lineno
        assert kw["require_all_sources"].value is True, call.lineno


def test_no_billing_handler_bumps_a_ctag():
    """Invoices, time, expenses, budgets and documents are not DAV-exposed:
    a CTag bump here would announce a sync that does not exist."""
    for fn in (handlers.preview_invoice, handlers._create_invoice_impl,
               handlers._update_invoice_impl, handlers._set_invoice_status,
               handlers._void_invoice, handlers._edit_invoice_draft,
               handlers.get_budget, handlers._create_budget_version_impl,
               handlers._invoice_note_document, handlers._billing_edit):
        source = inspect.getsource(fn)
        assert "bump_ctag" not in source and "_dav_resync" not in source, fn


# ══════════════════════════════════════════════════════════════════════
# 1. L'aperçu est le calcul de l'écriture
# ══════════════════════════════════════════════════════════════════════


def test_a_preview_writes_nothing_and_its_total_is_the_invoices(world):
    preview = _preview(time_entry_ids=["e1"], expense_ids=["x1"])
    assert world.commits == []
    assert world.peek(COUNTER) is None
    assert preview["ready"] is True and preview["refusals"] == []
    assert preview["line_count"] == 2
    assert preview["number_prefix"] == "2026-F"
    _conforms("preview_invoice", preview)

    payload = handlers.create_invoice({
        "dossier_id": "d1", "time_entry_ids": ["e1"], "expense_ids": ["x1"],
        "expected_total_cents": preview["total_cents"],
        "idempotency_key": "cle-facture-apercu"})
    _conforms("create_invoice", payload)
    stored = world.peek(f"invoices/{payload['entity']['id']}")
    assert stored["total"] == preview["total_cents"]
    assert stored["status"] == "brouillon" and stored["amount_paid"] == 0
    assert stored["created_via"] == "mcp"
    assert stored["invoice_number"] == "2026-F001"
    assert payload["entity"]["etag"] == stored["etag"]
    assert any("DÉFINITIVEMENT" in w for w in payload["warnings"])
    assert world.peek("timeentries/e1")["invoiced"] is True
    assert world.peek(COUNTER)["seq"] == 1


_PARITY = {
    # name: (mutation of the world, selection)
    "deja_facturee": (
        lambda w: w.external_write("timeentries/e1", _entry(
            "e1", invoiced=True, invoice_id="iX")),
        {"time_entry_ids": ["e1"]}),
    "autre_dossier": (
        lambda w: w.seed("timeentries/e3", _entry("e3", dossier="d2")),
        {"time_entry_ids": ["e1", "e3"]}),
    "non_facturable": (
        lambda w: w.seed("timeentries/e4", _entry("e4", billable=False,
                                                  amount=0)),
        {"time_entry_ids": ["e4"]}),
    "introuvable": (lambda w: None, {"time_entry_ids": ["fantome"]}),
    "sans_client": (
        lambda w: w.seed("timeentries/e5", _entry("e5", dossier="d3")),
        {"dossier_id": "d3", "time_entry_ids": ["e5"]}),
    "numero_tps_vide": ("no_taxes", {"expense_ids": ["x1"]}),
}


@pytest.mark.parametrize("case", sorted(_PARITY))
def test_preview_refuses_exactly_what_create_refuses(world, monkeypatch, case):
    mutate, selection = _PARITY[case]
    if mutate == "no_taxes":
        monkeypatch.setattr(handlers, "cabinet_dict", lambda: {})
    else:
        mutate(world)
    world.reset_logs()
    args = dict(selection)
    dossier_id = args.pop("dossier_id", "d1")
    preview = handlers.preview_invoice({"dossier_id": dossier_id, **args})
    assert preview["ready"] is False and preview["refusals"], preview
    _conforms("preview_invoice", preview)
    assert world.commits == []

    with pytest.raises(tools.ToolArgumentError) as refused:
        handlers.create_invoice({
            "dossier_id": dossier_id, **args,
            "expected_total_cents": preview["total_cents"],
            "idempotency_key": f"cle-parite-{case}"})
    assert refused.value.reason == "invoice_refused"
    assert "aucun numéro n'a été consommé" in str(refused.value)
    assert world.peek_collection("invoices") == {}
    assert world.peek(COUNTER) is None


def test_the_preview_names_each_source_and_its_reason(world):
    world.external_write("timeentries/e1", _entry("e1", invoiced=True,
                                                  invoice_id="iX"))
    preview = _preview(time_entry_ids=["e1", "e2"])
    rows = {r["id"]: r for r in preview["sources"]}
    assert rows["e1"]["retained"] is False
    assert "iX" in rows["e1"]["reason"]
    assert rows["e2"]["retained"] is True and rows["e2"]["reason"] is None
    assert rows["e2"]["amount_cents"] == 15000


def test_a_selection_is_named_ids_or_all_unbilled_never_both(world):
    with pytest.raises(tools.ToolArgumentError, match="pas les deux"):
        _preview(time_entry_ids=["e1"], all_unbilled=True)
    with pytest.raises(tools.ToolArgumentError, match="en double"):
        _preview(time_entry_ids=["e1", "e1"])
    with pytest.raises(tools.ToolArgumentError, match="au moins"):
        _preview()


def test_all_unbilled_takes_every_billable_unbilled_source(world):
    world.seed("timeentries/e4", _entry("e4", billable=False, amount=0))
    preview = _preview(all_unbilled=True)
    assert {r["id"] for r in preview["sources"]} == {"e1", "e2", "x1"}
    assert preview["ready"] is True and preview["truncated"] is False


def test_all_unbilled_past_the_ceiling_is_truncated_and_refused(world, monkeypatch):
    monkeypatch.setattr(handlers, "INVOICE_SOURCES_MAX", 2)
    preview = _preview(all_unbilled=True)
    assert preview["truncated"] is True and preview["ready"] is False
    with pytest.raises(tools.ToolArgumentError, match="Plus de 2 sources"):
        _create(all_unbilled=True, expected_total_cents=preview["total_cents"])
    assert world.peek(COUNTER) is None


def test_a_future_date_is_refused(world):
    with pytest.raises(tools.ToolArgumentError, match="futur"):
        _preview(time_entry_ids=["e1"], date="2026-09-29")


# ══════════════════════════════════════════════════════════════════════
# 2. Un numéro ne se brûle jamais
# ══════════════════════════════════════════════════════════════════════


def test_a_create_that_would_burn_a_number_never_does(world):
    """A total that differs from the preview's is refused — BEFORE the
    counter is read, so the year's sequence does not move."""
    with pytest.raises(tools.ToolArgumentError, match="total attendu"):
        _create(time_entry_ids=["e1"], expected_total_cents=1)
    assert world.peek(COUNTER) is None
    assert world.peek_collection("invoices") == {}
    assert world.peek("timeentries/e1")["invoiced"] is False
    # The next real invoice takes F001: nothing was consumed.
    assert _issued(world)["invoice_number"] == "2026-F001"


def test_a_double_create_with_the_same_key_replays_the_first_invoice(world):
    preview = _preview(time_entry_ids=["e1"])
    args = {"time_entry_ids": ["e1"],
            "expected_total_cents": preview["total_cents"],
            "idempotency_key": "cle-facture-rejouee"}
    first = _create(**args)
    second = _create(**args)
    assert second["idempotent_replay"] is True
    assert second["entity"]["id"] == first["entity"]["id"]
    assert len(world.peek_collection("invoices")) == 1
    assert world.peek(COUNTER)["seq"] == 1
    _conforms("create_invoice", second)


def test_create_invoice_demands_its_key(world):
    schema = tools.TOOLS["create_invoice"]["input_schema"]
    assert "idempotency_key" in schema["required"]
    assert tools.TOOLS["create_invoice"]["idempotency"] == tools.IDEMPOTENCY_REQUIRED


def test_the_schema_offers_no_status_payment_retainer_or_number(world):
    props = tools.TOOLS["create_invoice"]["input_schema"]["properties"]
    for forbidden in ("status", "amount_paid", "paid_date", "invoice_number",
                      "retainer_applied_cents", "adjustment", "legacy_ref",
                      "billing_address"):
        assert forbidden not in props, forbidden


def test_a_client_holding_trust_funds_is_warned_never_deducted(world):
    world.external_write("dossiers/d1", {**world.peek("dossiers/d1"),
                                         "trust_balance_by_client": {"p1": 50000}})
    preview = _preview(time_entry_ids=["e1"])
    payload = _create(time_entry_ids=["e1"],
                      expected_total_cents=preview["total_cents"])
    assert any("fidéicommis" in w for w in payload["warnings"])
    stored = world.peek(f"invoices/{payload['entity']['id']}")
    assert stored["retainer_applied"] == 0
    assert stored["amount_due"] == stored["total"]


# ══════════════════════════════════════════════════════════════════════
# 3. La facture se tient — statut, annulation, brouillon
# ══════════════════════════════════════════════════════════════════════


def test_get_invoice_hands_the_etag_and_the_connector_transitions(world):
    invoice = _issued(world)
    got = handlers.get_invoice({"invoice_id": invoice["id"]})["invoice"]
    assert got["etag"] == invoice["etag"]
    assert got["connector_transitions"] == ["envoyée", "annulée"]
    assert got["void_reason"] is None and got["created_via"] == "mcp"
    _conforms("get_invoice", {"found": True, "invoice": got})


def test_promoting_a_draft_writes_the_status_and_hands_back_the_new_etag(world):
    invoice = _issued(world)
    iid = invoice["id"]
    world.reset_logs()
    payload = handlers.update_invoice({"invoice_id": iid, "status": "envoyée",
                                       "expected_etag": invoice["etag"]})
    _conforms("update_invoice", payload)
    stored = world.peek(f"invoices/{iid}")
    assert stored["status"] == "envoyée" and stored["updated_via"] == "mcp"
    assert payload["mode"] == "status" and payload["outcome"] == "applied"
    assert payload["entity"]["previous_status"] == "brouillon"
    assert payload["entity"]["etag"] == stored["etag"] != invoice["etag"]
    assert any("Rien n'a été envoyé" in w for w in payload["warnings"])
    (commit,) = world.commits
    assert commit.transaction is not None


def test_the_same_status_twice_writes_nothing_even_on_the_old_etag(world):
    """The no-op is judged BEFORE the etag: a replay of a promotion that
    succeeded is never a false conflict."""
    invoice = _issued(world)
    iid = invoice["id"]
    handlers.update_invoice({"invoice_id": iid, "status": "envoyée",
                             "expected_etag": invoice["etag"]})
    world.reset_logs()
    again = handlers.update_invoice({"invoice_id": iid, "status": "envoyée",
                                     "expected_etag": invoice["etag"]})
    assert again["outcome"] == "unchanged"
    assert world.commits == []
    _conforms("update_invoice", again)


def test_a_stale_etag_is_refused_and_nothing_is_written(world):
    invoice = _issued(world)
    iid = invoice["id"]
    before = world.peek(f"invoices/{iid}")
    world.reset_logs()
    with pytest.raises(tools.ToolArgumentError) as refused:
        handlers.update_invoice({"invoice_id": iid, "status": "envoyée",
                                 "expected_etag": "une-autre-version"})
    assert refused.value.reason == "stale_etag"
    assert world.peek(f"invoices/{iid}") == before and world.commits == []


def test_en_retard_only_once_the_due_date_has_passed(world):
    invoice = _issued(world)
    iid = invoice["id"]
    sent = handlers.update_invoice({"invoice_id": iid, "status": "envoyée",
                                    "expected_etag": invoice["etag"]})
    etag = sent["entity"]["etag"]
    with pytest.raises(tools.ToolArgumentError, match="pas passée"):
        handlers.update_invoice({"invoice_id": iid, "status": "en_retard",
                                 "expected_etag": etag})
    world.external_write(f"invoices/{iid}", {
        **world.peek(f"invoices/{iid}"),
        "due_date": datetime(2026, 9, 27, tzinfo=UTC)})
    late = handlers.update_invoice({"invoice_id": iid, "status": "en_retard",
                                    "expected_etag": _etag_of(world, iid)})
    assert late["outcome"] == "applied"
    assert world.peek(f"invoices/{iid}")["status"] == "en_retard"
    assert "envoyée" in late["connector_transitions"]


def test_a_draft_goes_to_envoyee_first(world):
    invoice = _issued(world)
    with pytest.raises(tools.ToolArgumentError, match="d'abord"):
        handlers.update_invoice({"invoice_id": invoice["id"],
                                 "status": "en_retard",
                                 "expected_etag": invoice["etag"]})


def test_a_void_releases_the_sources_keeps_the_number_and_its_reason(world):
    invoice = _issued(world)
    iid = invoice["id"]
    payload = handlers.update_invoice({
        "invoice_id": iid, "status": "annulée", "expected_etag": invoice["etag"],
        "void_reason": "Facturée par erreur au mauvais dossier."})
    _conforms("update_invoice", payload)
    stored = world.peek(f"invoices/{iid}")
    assert stored["status"] == "annulée"
    assert stored["void_reason"] == "Facturée par erreur au mauvais dossier."
    assert stored["voided_at"] is not None
    assert stored["invoice_number"] == "2026-F001"
    assert payload["released_time_entry_ids"] == ["e1"]
    assert payload["released_expense_ids"] == ["x1"]
    assert world.peek("timeentries/e1")["invoiced"] is False
    # REWRITTEN (fixups of lot 3 — one wording on every surface): « never
    # reassigned » is the COUNTER's promise; an IMPORTED number can be
    # imported again once the voided invoice is deleted in the application
    # — the warning says both, and which promise is which.
    assert any("la numérotation de l'année ne le réattribue jamais" in w
               for w in payload["warnings"])
    assert any("un numéro REPRIS de l'ancien système (import_invoice) ne "
               "peut être importé de nouveau qu'une fois la facture annulée "
               "supprimée" in w for w in payload["warnings"])
    # The next invoice takes F002: the voided one kept its number.
    assert _issued(world, time_entry_ids=["e2"], expense_ids=[])[
        "invoice_number"] == "2026-F002"
    # And its reason is on the sheet the lawyer reads.
    got = handlers.get_invoice({"invoice_id": iid})["invoice"]
    assert got["void_reason"] == "Facturée par erreur au mauvais dossier."


def test_a_void_is_refused_while_a_payment_stands(world):
    invoice = _issued(world)
    iid = invoice["id"]
    world.external_write(f"invoices/{iid}", {**world.peek(f"invoices/{iid}"),
                                             "status": "envoyée",
                                             "amount_paid": 10000})
    before = world.peek(f"invoices/{iid}")
    world.reset_logs()
    with pytest.raises(tools.ToolArgumentError, match="encaissement") as refused:
        handlers.update_invoice({"invoice_id": iid, "status": "annulée",
                                 "expected_etag": before["etag"],
                                 "void_reason": "Erreur."})
    assert refused.value.reason == "invoice_refused"
    assert world.peek(f"invoices/{iid}") == before
    assert world.peek("timeentries/e1")["invoiced"] is True
    assert world.commits == []
    got = handlers.get_invoice({"invoice_id": iid})["invoice"]
    assert "annulée" not in got["connector_transitions"]


def test_a_void_demands_a_reason_and_a_voided_one_is_unchanged(world):
    invoice = _issued(world)
    iid = invoice["id"]
    with pytest.raises(tools.ToolArgumentError, match="void_reason"):
        handlers.update_invoice({"invoice_id": iid, "status": "annulée",
                                 "expected_etag": invoice["etag"]})
    with pytest.raises(tools.ToolArgumentError, match="chevrons"):
        handlers.update_invoice({"invoice_id": iid, "status": "annulée",
                                 "expected_etag": invoice["etag"],
                                 "void_reason": "a <b> c"})
    handlers.update_invoice({"invoice_id": iid, "status": "annulée",
                             "expected_etag": invoice["etag"],
                             "void_reason": "Erreur."})
    world.reset_logs()
    again = handlers.update_invoice({"invoice_id": iid, "status": "annulée",
                                     "expected_etag": invoice["etag"],
                                     "void_reason": "Erreur."})
    assert again["outcome"] == "unchanged" and world.commits == []
    _conforms("update_invoice", again)


def test_a_draft_correction_replaces_only_what_it_names(world):
    invoice = _issued(world, notes="Avant")
    iid = invoice["id"]
    payload = handlers.update_invoice({"invoice_id": iid, "notes": "Après",
                                       "expected_etag": invoice["etag"]})
    _conforms("update_invoice", payload)
    stored = world.peek(f"invoices/{iid}")
    assert stored["notes"] == "Après"
    assert stored["total"] == invoice["total"]
    assert payload["mode"] == "draft" and payload["changed_fields"] == ["notes"]
    assert payload["entity"]["etag"] == stored["etag"]


def test_a_draft_correction_on_an_issued_invoice_is_refused(world):
    invoice = _issued(world)
    iid = invoice["id"]
    sent = handlers.update_invoice({"invoice_id": iid, "status": "envoyée",
                                    "expected_etag": invoice["etag"]})
    with pytest.raises(tools.ToolArgumentError, match="brouillon"):
        handlers.update_invoice({"invoice_id": iid, "notes": "x",
                                 "expected_etag": sent["entity"]["etag"]})


def test_one_change_per_call(world):
    invoice = _issued(world)
    with pytest.raises(tools.ToolArgumentError, match="Une modification par appel"):
        handlers.update_invoice({"invoice_id": invoice["id"],
                                 "status": "envoyée", "notes": "x",
                                 "expected_etag": invoice["etag"]})
    with pytest.raises(tools.ToolArgumentError, match="expected_etag"):
        handlers.update_invoice({"invoice_id": invoice["id"],
                                 "status": "envoyée"})


# ══════════════════════════════════════════════════════════════════════
# 4. Le budget s'ajoute
# ══════════════════════════════════════════════════════════════════════


def _budget(**args):
    return handlers.create_budget_version({"dossier_id": "d1", **args})


def test_get_budget_without_a_budget(world):
    payload = handlers.get_budget({"dossier_id": "d1"})
    assert payload["has_budget"] is False and payload["base_version"] == 0
    assert payload["latest"] is None and payload["versions"] == []
    _conforms("get_budget", payload)


def test_a_first_version_then_a_stale_base_is_refused(world):
    created = handlers.create_budget_version({
        "dossier_id": "d1", "base_version": 0, "mode": "replace",
        "lines": [{"sous_phase": "PRE-01", "hours": 10}]})
    _conforms("create_budget_version", created)
    assert created["outcome"] == "created" and created["budget"]["version"] == 1
    stored = world.peek(f"budgets/{created['entity']['id']}")
    assert stored["created_via"] == "mcp" and stored["hourly_rate"] == 30000
    assert world.peek("counters/budget-d1")["seq"] == 1

    with pytest.raises(tools.ToolArgumentError, match="v1, pas la v0") as refused:
        _budget(base_version=0, mode="replace",
                lines=[{"sous_phase": "PRE-01", "hours": 20}])
    assert refused.value.reason == "budget_version_conflict"
    assert len(world.peek_collection("budgets")) == 1


def test_an_identical_version_writes_nothing(world):
    _budget(base_version=0, mode="replace",
            lines=[{"sous_phase": "PRE-01", "hours": 10}])
    world.reset_logs()
    again = _budget(base_version=0, mode="replace",
                    lines=[{"sous_phase": "PRE-01", "hours": 10}])
    assert again["outcome"] == "unchanged" and again["created"] is False
    assert world.commits == []
    _conforms("create_budget_version", again)


def test_merge_replaces_named_lines_removes_zeros_keeps_the_rest(world):
    _budget(base_version=0, mode="replace",
            lines=[{"sous_phase": "PRE-01", "hours": 10},
                   {"sous_phase": "INT-01", "hours": 4}])
    merged = _budget(base_version=1, mode="merge",
                     lines=[{"sous_phase": "PRE-01", "hours": 0},
                            {"sous_phase": "INT-01", "hours": 6},
                            {"sous_phase": "AUD-01", "hours": 2,
                             "frais_cents": 5000}])
    assert merged["diff"] == {"added": ["AUD-01"], "changed": ["INT-01"],
                              "removed": ["PRE-01"]}
    codes = [l["sous_phase"] for l in merged["budget"]["lines"]]
    assert codes == ["AUD-01", "INT-01"]
    assert any("PRE-01" in w for w in merged["warnings"])
    view = handlers.get_budget({"dossier_id": "d1"})
    assert view["base_version"] == 2 and len(view["versions"]) == 2
    _conforms("get_budget", view)


def test_budget_lines_are_refused_rather_than_rounded(world):
    schema = tools.TOOLS["create_budget_version"]["input_schema"]
    assert tools.validate_args(schema, {
        "dossier_id": "d1", "base_version": 0, "mode": "replace",
        "lines": [{"sous_phase": "ADM-00", "hours": 1}]})
    with pytest.raises(tools.ToolArgumentError, match="deux décimales"):
        _budget(base_version=0, mode="replace",
                lines=[{"sous_phase": "PRE-01", "hours": 1.234}])
    with pytest.raises(tools.ToolArgumentError, match="double"):
        _budget(base_version=0, mode="replace",
                lines=[{"sous_phase": "PRE-01", "hours": 1},
                       {"sous_phase": "PRE-01", "hours": 2}])
    assert world.peek_collection("budgets") == {}


# ══════════════════════════════════════════════════════════════════════
# 5. Le déplacement — la règle unique de models/billing_move
# ══════════════════════════════════════════════════════════════════════


def test_a_move_only_rewrites_the_dossier_link(world):
    before = world.peek("timeentries/e2")
    payload = handlers.update_time_entry({"time_entry_id": "e2",
                                          "dossier_id": "d2"})
    _conforms("update_time_entry", payload)
    stored = world.peek("timeentries/e2")
    assert stored["dossier_id"] == "d2"
    assert stored["dossier_file_number"] == "2026-d2"
    for key in ("hours", "rate", "amount", "description", "phase"):
        assert stored[key] == before[key], key
    assert payload["moved"] is True and payload["previous_dossier_id"] == "d1"
    assert payload["entity"]["etag"] == stored["etag"]
    # The target's rate differs: said, never applied.
    assert any("taux" in w for w in payload["warnings"])


def test_a_move_with_a_field_is_one_write(world):
    world.reset_logs()
    payload = handlers.update_expense({"expense_id": "x1", "dossier_id": "d2",
                                       "description": "Timbre (corrigé)"})
    _conforms("update_expense", payload)
    stored = world.peek("expenses/x1")
    assert stored["dossier_id"] == "d2"
    assert stored["description"] == "Timbre (corrigé)"
    assert len([c for c in world.commits
                if any(p == "expenses/x1" for _k, p in c.ops)]) == 1


def test_a_move_to_the_same_dossier_writes_nothing(world):
    world.reset_logs()
    payload = handlers.update_time_entry({"time_entry_id": "e1",
                                          "dossier_id": "d1"})
    assert payload["moved"] is False and world.commits == []
    _conforms("update_time_entry", payload)


def test_a_move_is_refused_to_an_unknown_dossier_or_of_a_billed_entry(world):
    with pytest.raises(tools.ToolArgumentError, match="Dossier introuvable"):
        handlers.update_time_entry({"time_entry_id": "e1",
                                    "dossier_id": "nulle-part"})
    world.external_write("timeentries/e1", _entry("e1", invoiced=True,
                                                  invoice_id="iX"))
    with pytest.raises(tools.ToolArgumentError, match="annulez la facture"):
        handlers.update_time_entry({"time_entry_id": "e1",
                                    "dossier_id": "d2"})
    assert world.peek("timeentries/e1")["dossier_id"] == "d1"


# ══════════════════════════════════════════════════════════════════════
# 6. La note d'honoraires
# ══════════════════════════════════════════════════════════════════════


_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)


def _note_template(world) -> dict:
    body = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
        'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
        '<w:t>Note {{facture.numero}} — {{facture.total_apres_taxes}}</w:t>'
        '</w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml", body)
    data = buf.getvalue()
    template, errors = tpl_model.create_template(
        io.BytesIO(data), "note.docx", len(data),
        {"name": "Note d'honoraires", "category": "correspondance",
         "kind": "note_honoraires"}, UID)
    assert errors == [], errors
    designated, errors = tpl_model.set_active_template(
        template["id"], par="juriste", expected_etag=None)
    assert errors == [], errors
    return designated


def test_the_invoice_note_is_generated_then_returned_when_identical(world):
    template = _note_template(world)
    invoice = _issued(world)
    first = handlers.create_document({"source": "invoice_note",
                                      "invoice_id": invoice["id"]})
    _conforms("create_document", first)
    assert first["reused"] is False
    assert first["template"]["id"] == template["id"]
    assert first["invoice"]["invoice_number"] == "2026-F001"
    doc = world.peek(f"documents/{first['entity']['id']}")
    assert doc["source_invoice_id"] == invoice["id"]
    assert "par Claude (connecteur)" in doc["genere_depuis"]
    assert doc["storage_path"].startswith(f"users/{UID}/")
    assert first["folder"]["system_role"] == "projets"

    second = handlers.create_document({"source": "invoice_note",
                                       "invoice_id": invoice["id"]})
    _conforms("create_document", second)
    assert second["reused"] is True
    assert second["entity"]["id"] == first["entity"]["id"]
    assert len(world.peek_collection("documents")) == 1

    third = handlers.create_document({"source": "invoice_note",
                                      "invoice_id": invoice["id"],
                                      "regenerate": True})
    assert third["reused"] is False
    assert len(world.peek_collection("documents")) == 2


def test_the_invoice_note_is_refused_without_a_designated_template(world):
    invoice = _issued(world)
    with pytest.raises(tools.ToolArgumentError, match="désigné"):
        handlers.create_document({"source": "invoice_note",
                                  "invoice_id": invoice["id"]})
    assert world.peek_collection("documents") == {}


def test_the_invoice_note_takes_no_folder_nor_dossier(world):
    with pytest.raises(tools.ToolArgumentError, match="ne s'applique pas"):
        handlers.create_document({"source": "invoice_note",
                                  "invoice_id": "i1", "folder_id": ""})


# ══════════════════════════════════════════════════════════════════════
# 7. Aucune sortie ne porte une URL ni un chemin de stockage
# ══════════════════════════════════════════════════════════════════════


def test_no_billing_payload_carries_a_url_or_a_storage_path(world):
    _note_template(world)
    invoice = _issued(world)
    payloads = [
        _preview(time_entry_ids=["e2"]),
        handlers.get_budget({"dossier_id": "d1"}),
        handlers.update_invoice({"invoice_id": invoice["id"],
                                 "notes": "Note",
                                 "expected_etag": invoice["etag"]}),
        handlers.create_document({"source": "invoice_note",
                                  "invoice_id": invoice["id"]}),
    ]
    for payload in payloads:
        blob = json.dumps(tools._jsonable(payload), ensure_ascii=False)
        assert "storage_path" not in blob and "X-Goog" not in blob
        assert "http" not in blob, blob[:200]


def test_every_new_tool_is_described_in_the_registry():
    for name in ("preview_invoice", "get_budget", "create_invoice",
                 "update_invoice", "create_budget_version"):
        assert name in tools.TOOLS and name in OUTPUT_SCHEMAS, name
    assert {"create_invoice", "update_invoice",
            "create_budget_version"} <= tools.EDIT_TOOLS
    assert "preview_invoice" not in tools.WRITE_TOOLS
    assert "get_budget" not in tools.WRITE_TOOLS


# ══════════════════════════════════════════════════════════════════════
# 8. Revue adversariale du lot 3b — chaque défaut, son test (chacun
#    échoue sur le code d'avant la revue)
# ══════════════════════════════════════════════════════════════════════


class _FailingQueries:
    """A store whose QUERIES on one collection fail while its documents stay
    readable by id — exactly the outage a fail-open list reader swallows
    into ``[]`` (``list_time_entries``' ``except Exception: return []``)."""

    def __init__(self, real, collection: str) -> None:
        self._real, self._collection = real, collection

    def collection(self, name: str):
        col = self._real.collection(name)
        return _FailingCollection(col) if name == self._collection else col

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class _FailingCollection:
    def __init__(self, col) -> None:
        self._col = col

    def where(self, *_a, **_k):
        return self

    def stream(self, *_a, **_k):
        raise RuntimeError("store unavailable")

    def __getattr__(self, name: str):
        return getattr(self._col, name)


def _time_queries_fail(world, monkeypatch) -> None:
    module = sys.modules["models.time_entry"]
    monkeypatch.setattr(module, "db", _FailingQueries(module.db, "timeentries"))


def test_all_unbilled_refuses_when_the_time_entries_cannot_be_read(
        world, monkeypatch):
    """Read fail-open, an outage made « every unbilled source » mean « every
    unbilled DISBURSEMENT »: the preview agreed, the invoice was ISSUED
    without the dossier's time and took a permanent number for it."""
    _time_queries_fail(world, monkeypatch)
    with pytest.raises(tools.ToolArgumentError, match="lecture impossible") as r:
        _preview(all_unbilled=True)
    assert r.value.reason == "invoice_refused"
    with pytest.raises(tools.ToolArgumentError, match="lecture impossible"):
        _create(all_unbilled=True, expected_total_cents=5749)
    assert world.peek_collection("invoices") == {}
    assert world.peek(COUNTER) is None
    assert world.peek("expenses/x1")["invoiced"] is False


def test_get_budget_refuses_when_the_actuals_cannot_be_read(world, monkeypatch):
    """Read fail-open, an outage showed NO consumption: every phase « ok »,
    the 80 % alert — the deontological trigger — silently gone."""
    _budget(base_version=0, mode="replace",
            lines=[{"sous_phase": "PRE-01", "hours": 1}])
    _time_queries_fail(world, monkeypatch)
    with pytest.raises(tools.ToolArgumentError, match="lecture impossible"):
        handlers.get_budget({"dossier_id": "d1"})


def test_merge_keeps_the_values_a_line_does_not_name(world):
    """A merge line sent to raise the hours used to ZERO the phase's frais —
    a quoted figure dropped from the client's estimate in silence."""
    _budget(base_version=0, mode="replace",
            lines=[{"sous_phase": "PRE-01", "hours": 10, "frais_cents": 5000},
                   {"sous_phase": "INT-01", "hours": 4}])
    merged = _budget(base_version=1, mode="merge",
                     lines=[{"sous_phase": "PRE-01", "hours": 12}])
    stored = world.peek(f"budgets/{merged['entity']['id']}")
    lines = {l["sous_phase"]: l for l in stored["lines"]}
    assert lines["PRE-01"]["hours"] == 12 and lines["PRE-01"]["frais_cents"] == 5000
    assert lines["INT-01"]["hours"] == 4
    assert merged["diff"] == {"added": [], "changed": ["PRE-01"], "removed": []}
    # A frais-only line keeps the hours.
    again = _budget(base_version=2, mode="merge",
                    lines=[{"sous_phase": "INT-01", "frais_cents": 700}])
    lines = {l["sous_phase"]: l
             for l in world.peek(f"budgets/{again['entity']['id']}")["lines"]}
    assert lines["INT-01"]["hours"] == 4 and lines["INT-01"]["frais_cents"] == 700


def test_a_budget_line_naming_no_figure_is_refused(world):
    """It used to REMOVE the line in merge mode (read as 0 h, 0 ¢)."""
    _budget(base_version=0, mode="replace",
            lines=[{"sous_phase": "PRE-01", "hours": 10}])
    world.reset_logs()
    with pytest.raises(tools.ToolArgumentError, match="aucun chiffre"):
        _budget(base_version=1, mode="merge", lines=[{"sous_phase": "PRE-01"}])
    assert world.commits == []
    assert len(world.peek_collection("budgets")) == 1


def test_a_reused_invoice_note_says_nothing_was_created(world):
    _note_template(world)
    invoice = _issued(world)
    first = handlers.create_document({"source": "invoice_note",
                                      "invoice_id": invoice["id"]})
    assert first["created"] is True
    second = handlers.create_document({"source": "invoice_note",
                                       "invoice_id": invoice["id"]})
    assert second["reused"] is True and second["created"] is False
    _conforms("create_document", second)


def test_replaying_a_move_with_its_pre_move_etag_writes_nothing(world):
    """The move regenerates the etag; the replay of the SAME call (no key)
    used to be refused as a false conflict — the model's own move answers
    « already there » before the etag, and now the handler does too."""
    args = {"time_entry_id": "e2", "dossier_id": "d2",
            "expected_etag": "e2-e0"}
    handlers.update_time_entry(dict(args))
    world.reset_logs()
    again = handlers.update_time_entry(dict(args))
    assert again["moved"] is False and world.commits == []
    assert world.peek("timeentries/e2")["dossier_id"] == "d2"
    _conforms("update_time_entry", again)


def test_move_warnings_speak_of_the_row_as_written(world):
    """The rate warning read the PRE-write row: a move that also set the
    target's rate still said « the entry's rate is kept, it differs »."""
    payload = handlers.update_time_entry({"time_entry_id": "e2",
                                          "dossier_id": "d2",
                                          "rate_cents": 25000})
    assert world.peek("timeentries/e2")["rate"] == 25000
    assert not any("taux" in w for w in payload["warnings"])
    # A non-billable entry counts in no budget: no budget warning.
    world.seed("timeentries/e6", _entry("e6", billable=False, amount=0))
    moved = handlers.update_time_entry({"time_entry_id": "e6",
                                        "dossier_id": "d2"})
    assert moved["moved"] is True
    assert not any("budget" in w for w in moved["warnings"])


def test_a_source_changed_during_the_call_burns_no_number(world, monkeypatch):
    """The burn the plan names: a source edited between the plan's read and
    the invoice transaction. The number is drawn INSIDE that transaction,
    so its abort leaves the year's counter where it was — the total-mismatch
    refusal above never even reaches the counter."""
    preview = _preview(time_entry_ids=["e1"])
    real_plan = invoice_model.plan_invoice

    def plan_then_concurrent_edit(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        world.external_write("timeentries/e1", {
            **world.peek("timeentries/e1"), "hours": 2.0,
            "etag": "e1-concurrent"})
        return plan

    monkeypatch.setattr(invoice_model, "plan_invoice", plan_then_concurrent_edit)
    with pytest.raises(tools.ToolArgumentError, match="entre-temps") as refused:
        _create(time_entry_ids=["e1"],
                expected_total_cents=preview["total_cents"])
    assert "aucun numéro n'a été consommé" in str(refused.value)
    assert world.peek(COUNTER) is None
    assert world.peek_collection("invoices") == {}
    assert world.peek("timeentries/e1")["invoiced"] is False


# ══════════════════════════════════════════════════════════════════════
# Correctifs du lot 3 — l'issue incertaine ne se dit jamais « rien créé »
# ══════════════════════════════════════════════════════════════════════


def _lose_the_answer_of_the_invoice_commit(world, monkeypatch):
    """The invoice transaction's commit LANDS, then the call raises — the
    answer lost after the server applied it (a reset connection)."""
    server = world._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(
                "/invoices/" in server._write_name(w) for w in writes):
            state["armed"] = False
            raise RuntimeError("connection reset after the commit")
        return response

    monkeypatch.setattr(server, "commit", _commit)


def test_an_uncertain_create_never_says_no_number_was_consumed(
    world, monkeypatch,
):
    """The model's generic transaction error is AMBIGUOUS: here the invoice
    WAS written and its number consumed. The old handler appended « aucun
    numéro n'a été consommé » to it and released the claim, so the same-key
    retry issued a SECOND invoice under the next number. Both halves fail
    on the old code."""
    preview = _preview(time_entry_ids=["e1"])
    args = {"time_entry_ids": ["e1"],
            "expected_total_cents": preview["total_cents"],
            "idempotency_key": "cle-facture-incertaine"}
    _lose_the_answer_of_the_invoice_commit(world, monkeypatch)
    with pytest.raises(tools.ToolArgumentError) as uncertain:
        _create(**args)
    message = str(uncertain.value)
    assert uncertain.value.reason == "invoice_outcome_uncertain"
    assert "aucun numéro n'a été consommé" not in message
    assert "Aucune facture n'a été créée" not in message
    assert "INCERTAINE" in message and "list_invoices" in message
    assert "MÊME idempotency_key" in message
    # It was written — which is exactly why the text may not deny it.
    (written,) = world.peek_collection("invoices").values()
    assert written["invoice_number"] == "2026-F001"
    assert world.peek(COUNTER)["seq"] == 1

    # The same key cannot issue a second invoice: its claim was KEPT.
    with pytest.raises(tools.ToolArgumentError) as retried:
        _create(**args)
    assert retried.value.reason == "idempotency_in_flight"
    assert len(world.peek_collection("invoices")) == 1
    assert world.peek(COUNTER)["seq"] == 1


def test_the_model_answers_an_uncertain_outcome_by_its_constant(
    world, monkeypatch,
):
    """The web form shows the model's own text: it never claims nothing
    was created either (it used to read « Erreur lors de la sauvegarde.
    Veuillez réessayer. », an invitation to issue the invoice twice)."""
    preview = _preview(time_entry_ids=["e1"])
    answers = []
    real_create = invoice_model.create_invoice

    def recording(*args, **kwargs):
        answers.append(real_create(*args, **kwargs))
        return answers[-1]

    monkeypatch.setattr(invoice_model, "create_invoice", recording)
    _lose_the_answer_of_the_invoice_commit(world, monkeypatch)
    with pytest.raises(tools.ToolArgumentError):
        _create(time_entry_ids=["e1"],
                expected_total_cents=preview["total_cents"])
    ((invoice, errors),) = answers
    assert invoice is None
    assert errors == [invoice_model.CREATE_OUTCOME_UNCERTAIN]
    assert "vérifiez la liste des factures" in errors[0]
    assert "Veuillez réessayer" not in errors[0]


def test_a_certain_refusal_still_says_no_number_was_consumed(world):
    """A refusal of the plan precedes the counter: there the suffix is true,
    and stays."""
    with pytest.raises(tools.ToolArgumentError) as refused:
        _create(time_entry_ids=["e1"], expected_total_cents=1)
    assert refused.value.reason == "invoice_refused"
    assert "aucun numéro n'a été consommé" in str(refused.value)
    assert refused.value.keep_claim is False


def test_imp07_names_the_imported_brouillon_never_the_issued_one(world):
    """The real store, both creators (fixups of lot 3): create_invoice
    stamps `imported: False`, import_invoice `imported: True` — decided by
    the model from whether a number was carried over — and get_import_audit
    flags only the reprise. FAILS on the old IMP-07, which named both."""
    issued = _issued(world)
    assert issued["imported"] is False
    total = invoice_model.compute_totals(
        [{"type": "fee", "amount": 15000, "taxable": True}])["total"]
    imported = handlers.import_invoice({
        "dossier_id": "d1", "invoice_number": "2019-F014",
        "date": "2019-11-04", "time_entry_ids": ["e2"],
        "expected_total_cents": total,
    })
    stored = world.peek(f"invoices/{imported['entity']['id']}")
    assert stored["imported"] is True
    findings = {f["code"]: f for f in handlers.get_import_audit(
        {"dossier_id": "d1"})["findings"]}
    detail = findings["IMP-07"]["detail"]
    assert "2019-F014" in detail
    assert issued["invoice_number"] not in detail


def test_a_caller_cannot_mark_an_invoice_imported(world):
    """The marker is the model's answer, never a data key: `imported` is not
    in _CREATE_DATA_KEYS, so a caller's True is dropped."""
    assert "imported" not in invoice_model._CREATE_DATA_KEYS
    doc = invoice_model.invoice_document_from({"imported": True})
    assert doc["imported"] is False


# ── Revue des correctifs du lot 3 — la reprise d'une transaction déjà écrite ──


def _abort_after_the_invoice_commit_lands(world, monkeypatch):
    """The invoice commit LANDS, then the call is answered ``Aborted`` — the
    commit RPC retried under the same transaction id after its first answer
    was lost. The REAL ``transactional`` decorator then re-runs the body
    over the writes that landed."""
    from google.api_core import exceptions as gexc

    server = world._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(
                "/invoices/" in server._write_name(w) for w in writes):
            state["armed"] = False
            raise gexc.Aborted("the retried commit of a landed transaction")
        return response

    monkeypatch.setattr(server, "commit", _commit)


def test_a_rerun_over_its_own_landed_commit_is_uncertain_never_a_conflict(
    world, monkeypatch,
):
    """Review of the fixups of lot 3. The re-run found its sources invoiced
    — by ITSELF — and answered the source conflict, to which the handler
    appended « Aucune facture n'a été créée — aucun numéro n'a été
    consommé », and released the key: false, over an invoice that exists
    under a consumed number. The call's own invoice id tells the two apart.
    FAILS on the old model."""
    preview = _preview(time_entry_ids=["e1"])
    args = {"time_entry_ids": ["e1"],
            "expected_total_cents": preview["total_cents"],
            "idempotency_key": "cle-facture-reprise-ecrite"}
    _abort_after_the_invoice_commit_lands(world, monkeypatch)
    with pytest.raises(tools.ToolArgumentError) as uncertain:
        _create(**args)
    assert uncertain.value.reason == "invoice_outcome_uncertain"
    assert "aucun numéro n'a été consommé" not in str(uncertain.value)
    assert "entre-temps" not in str(uncertain.value)
    (written,) = world.peek_collection("invoices").values()
    assert written["invoice_number"] == "2026-F001"
    assert world.peek(COUNTER)["seq"] == 1
    assert world.peek("timeentries/e1")["invoice_id"] == written["id"]
    # The key stays reserved: the same-key retry never runs.
    with pytest.raises(tools.ToolArgumentError) as retried:
        _create(**args)
    assert retried.value.reason == "idempotency_in_flight"
    assert len(world.peek_collection("invoices")) == 1


def test_an_import_rerun_over_its_own_landed_commit_is_uncertain(
    world, monkeypatch,
):
    """The import path: the re-run's source check sees its own id too, and
    its uniqueness read would have found its OWN invoice (« ce numéro existe
    déjà »). The model answers the uncertain outcome, never a conflict."""
    total = invoice_model.compute_totals(
        [{"type": "fee", "amount": 15000, "taxable": True}])["total"]
    _abort_after_the_invoice_commit_lands(world, monkeypatch)
    with pytest.raises(tools.ToolArgumentError) as uncertain:
        handlers.import_invoice({
            "dossier_id": "d1", "invoice_number": "2019-F015",
            "date": "2019-11-04", "time_entry_ids": ["e2"],
            "expected_total_cents": total,
        })
    assert str(uncertain.value) == invoice_model.CREATE_OUTCOME_UNCERTAIN
    (written,) = world.peek_collection("invoices").values()
    assert written["invoice_number"] == "2019-F015"
    assert world.peek(COUNTER) is None     # the year counter never moved
