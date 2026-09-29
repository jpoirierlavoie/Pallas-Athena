"""Le connecteur tient la comptabilité — lot 5b (plan D1, D2, D14, D16).

Six outils sous le scope DISTINCT ``athena:comptabilite`` (et son propre
interrupteur, ``MCP_COMPTABILITE_ENABLED``, faux par défaut) :

* ``get_admin_ledger`` — la LECTURE du registre d'administration ;
* ``record_trust_entry`` — une écriture au fidéicommis, dont le paiement
  d'honoraires ATOMIQUE (retrait, recette, paiement sur la facture) ;
* ``record_admin_entry`` — une écriture d'administration (dépense ventilée,
  autre recette, encaissement qui paie la facture, paiement de carte) ;
* ``update_admin_entry`` — la correction d'une écriture encore modifiable,
  contre l'etag lu ;
* ``clear_register_entries`` — la compensation à la date du RELEVÉ ;
* ``reverse_register_entry`` — la contre-passation, seule correction d'un
  registre.

Tout passe par les VRAIS gestionnaires, le VRAI service
(``services/comptabilite``, la porte des routes web) et les VRAIS modèles
au-dessus du faux Firestore partagé : on relit ce qui est STOCKÉ. Plus les
trois tests comportementaux que trois promesses du registre
(``mcp/disclosure.NEVERS``) nomment, les deux preuves de la carte des
formulaires web (``tests/test_edit_conflict_web.TRANSITIONS``), et une
conformité d'``outputSchema`` par outil et par branche.
"""

import ast
import itertools
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
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
    import mcp.write_support as write_support
    from models import admin_ledger as al
    from models import fee_payment
    from models import settings as settings_model  # noqa: F401 — faked below
    from models import trust
    from services import comptabilite as svc

from tests._fake_firestore import install  # noqa: E402
from tests.test_mcp_output_schemas import _conforms  # noqa: E402

UTC = timezone.utc
TRANSIT = "12345"
LAST4 = "6789"
RECEIPT_PATH = "users/kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s/administration/rx/recu.pdf"


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    """The two registers, one dossier with two clients, an issued invoice,
    and the Montréal clock frozen on 2026-09-20 (noon)."""
    from utils import deadlines as dl

    frozen = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    f = install(monkeypatch, *_fake_modules())
    # The firm profile as « Paramètres » stores it — the payees a fee
    # payment may name (D23, art. 58), read by the MODEL through
    # utils/cabinet (rewritten deliberately: a monkeypatch of the
    # handler's cabinet_dict stood in for it while the payee was the
    # handler's rule alone).
    f.seed("settings/cabinet", {"nom": "Me Jason Poirier Lavoie",
                                "organisation": "Poirier Lavoie, avocat"})
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "institution": "Desjardins",
        "status": "actif", "account_type": "général", "transit": TRANSIT,
        "account_number_last4": LAST4, "book_balance": 0, "bank_balance": 0,
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "status": "actif", "client_ids": ["c1", "c2"],
        "clients": [{"id": "c1", "name": "Jean Tremblay"},
                    {"id": "c2", "name": "Marie Tremblay"}],
        "trust_balance": 0, "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
    })
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "institution": "Desjardins",
        "status": "actif", "account_type": "opérations", "transit": TRANSIT,
        "account_number_last4": LAST4, "ledger_balance": 0,
    })
    f.seed("admin_accounts/card1", {
        "id": "card1", "name": "Carte", "institution": "Visa",
        "status": "actif", "account_type": "carte_crédit",
        "account_number_last4": LAST4, "ledger_balance": 0,
    })
    f.seed("invoices/inv1", {
        "id": "inv1", "invoice_number": "2026-F040", "dossier_id": "dos1",
        "dossier_file_number": "2026-001", "client_id": "c1",
        "client_name": "Jean Tremblay", "status": "envoyée", "total": 100000,
        "retainer_applied": 0, "amount_due": 100000, "amount_paid": 0,
    })
    f.seed("invoices/inv2", {
        "id": "inv2", "invoice_number": "2026-F041", "dossier_id": "dos1",
        "dossier_file_number": "2026-001", "client_id": "c1",
        "status": "envoyée", "total": 100000, "retainer_applied": 20000,
        "amount_due": 80000, "amount_paid": 0,
    })
    f.seed("invoices/inv3", {
        "id": "inv3", "invoice_number": "2026-F042", "dossier_id": "dos1",
        "client_id": "c1", "status": "brouillon", "total": 50000,
        "retainer_applied": 0, "amount_due": 50000, "amount_paid": 0,
    })
    return f


_KEYS = itertools.count(1)


def _key() -> str:
    return f"cle-comptable-{next(_KEYS):04d}"


def _call(tool: str, **args) -> dict:
    """A tool call as tools/call makes it: the input schema first (a caller
    cannot send what it refuses), then the REAL handler."""
    if tool in tools.WRITE_TOOLS:
        args.setdefault("idempotency_key", _key())
    errors = tools.validate_args(tools.TOOLS[tool]["input_schema"], args)
    assert errors == [], errors
    return getattr(handlers, tools.TOOLS[tool]["handler"])(args)


def _refused(tool: str, **args) -> tools.ToolArgumentError:
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        _call(tool, **args)
    return excinfo.value


def _deposit(amount: int = 200000, day: str = "2026-09-02", **over) -> dict:
    args = {"account_id": "acc1", "direction": "recette",
            "purpose": "dépôt_client", "amount_cents": amount, "date": day,
            "method": "chèque", "dossier_id": "dos1", "client_id": "c1",
            "counterparty": "Jean Tremblay"}
    args.update(over)
    return args


def _cleared_deposit(fake, amount: int = 200000) -> str:
    """A deposit recorded AND cleared through the connector."""
    rec = _call("record_trust_entry", **_deposit(amount))
    _call("clear_register_entries", register="trust",
          tx_ids=[rec["entity"]["id"]], cleared_date="2026-09-03")
    return rec["entity"]["id"]


def _fee(amount: int = 100000, **over) -> dict:
    args = {"account_id": "acc1", "direction": "déboursé",
            "purpose": "virement_honoraires", "amount_cents": amount,
            "date": "2026-09-10", "method": "virement", "dossier_id": "dos1",
            "client_id": "c1", "invoice_id": "inv1",
            "admin_account_id": "ops1"}
    args.update(over)
    return args


def _depense(amount: int = 11498, **over) -> dict:
    args = {"account_id": "ops1", "kind": "dépense", "amount_cents": amount,
            "date": "2026-09-05", "method": "virement", "category": "loyer",
            "ventilation": "ventiler", "counterparty": "Immeubles X"}
    args.update(over)
    return args


def _entries(fake, collection: str) -> dict:
    return fake.peek_collection(collection)


def _etags(fake, *tx_ids: str) -> list:
    """The STORED etag of each administration entry — what a fresh
    get_admin_ledger read would hand the caller for `expected_etags`."""
    return [fake.peek(f"admin_transactions/{t}")["etag"] for t in tx_ids]


# ══════════════════════════════════════════════════════════════════════
# 0. Le registre des outils — ce qu'il déclare, les modèles le tiennent
# ══════════════════════════════════════════════════════════════════════


def test_the_hand_copied_vocabularies_are_the_models_own():
    """mcp/tools.py cannot import a model (Firestore at import): its enums
    are literals, pinned here against the models they copy."""
    assert tools._TRUST_DIRECTIONS == list(trust.VALID_DIRECTIONS)
    assert tools._TRUST_METHODS == list(trust.VALID_METHODS)
    assert set(tools._TRUST_ENTRY_PURPOSES) == (
        set(trust.VALID_PURPOSES) - {trust.REVERSAL_PURPOSE, trust.TRANSFER_PURPOSE})
    assert tools._ADMIN_KINDS == list(al.VALID_KINDS)
    assert set(tools._ADMIN_ENTRY_KINDS) == set(al.VALID_KINDS) - {al.REVERSAL_KIND}
    assert set(tools._ADMIN_EDIT_KINDS) == set(al._EDITABLE_KINDS)
    assert tools._ADMIN_METHODS == list(al.VALID_METHODS)
    assert tools._ADMIN_CATEGORIES == list(al.ADMIN_EXPENSE_CATEGORIES)
    assert tools._REGISTER_STATUSES == list(trust.VALID_TX_STATUSES)
    assert tools._REGISTER_STATUSES == list(al.VALID_TX_STATUSES)


def test_the_objet_sens_map_is_the_models_one_source():
    """Revue de complétude du lot 5 : la carte objet → sens vivait dans le
    gestionnaire seul, et le contrôle d'intégrité qui doit MESURER
    l'historique avant que l'avocat étende la règle au web ne pouvait pas la
    lire sans en tenir une copie. Elle est le vocabulaire du modèle ; le
    connecteur la lit par le service."""
    from scripts import verify_trust_integrity as vti

    assert handlers._TRUST_PURPOSE_DIRECTION == trust.PURPOSE_DIRECTIONS
    assert svc.TRUST_PURPOSE_DIRECTIONS == trust.PURPOSE_DIRECTIONS
    assert vti.trust.PURPOSE_DIRECTIONS is trust.PURPOSE_DIRECTIONS
    assert set(trust.PURPOSE_DIRECTIONS) <= set(trust.VALID_PURPOSES)
    assert set(trust.PURPOSE_DIRECTIONS.values()) <= set(trust.VALID_DIRECTIONS)
    reserved = {trust.REVERSAL_PURPOSE, trust.TRANSFER_PURPOSE, trust.FEE_PAYMENT_PURPOSE}
    assert not set(trust.PURPOSE_DIRECTIONS) & reserved


def test_the_connector_never_records_a_transfer_nor_a_correction_by_itself():
    purposes = tools.TOOLS["record_trust_entry"]["input_schema"]["properties"][
        "purpose"]["enum"]
    assert "correction" not in purposes and "virement_inter_dossiers" not in purposes
    kinds = tools.TOOLS["record_admin_entry"]["input_schema"]["properties"][
        "kind"]["enum"]
    assert "correction" not in kinds


def test_the_accounting_writes_demand_their_key_and_their_scope():
    for name in tools.ACCOUNTING_WRITE_TOOLS:
        spec = tools.TOOLS[name]
        assert spec["scope"] == "athena:comptabilite", name
        assert spec["idempotency"] == tools.IDEMPOTENCY_REQUIRED, name
        assert "idempotency_key" in spec["input_schema"]["required"], name
    assert tools.TOOLS["get_admin_ledger"]["scope"] == "athena:comptabilite"
    assert "idempotency" not in tools.TOOLS["get_admin_ledger"]


def test_the_write_protocol_names_every_required_tool():
    """Review of lot 5, step 5 (concurrency lens): the protocol's own text
    (mcp/write_support.py, § 3 « Failure posture ») listed the tools whose
    claim fails CLOSED as three — create_hearing_series, decide_rendez_vous,
    create_invoice — after lot 5b had made the five accounting writes
    ``required`` too, and mcp/tools.py said « every write tool but the two
    below ». DERIVED from the registry, so the next ``required`` tool cannot
    ship with the protocol's description of it stale."""
    required = sorted(n for n, s in tools.TOOLS.items()
                      if s.get("idempotency") == tools.IDEMPOTENCY_REQUIRED)
    assert set(tools.ACCOUNTING_WRITE_TOOLS) <= set(required)
    posture = write_support.__doc__.split("3. Failure posture", 1)[1].split(
        "4. What is stored", 1)[0]
    for name in required:
        assert f"``{name}``" in posture, name
    words = {3: "three", 8: "eight"}
    assert f"every write tool but the {words.get(len(required), len(required))} " \
           f"``required`` ones below" in " ".join(posture.split())
    source = (_ATHENA / "mcp" / "tools.py").read_text(encoding="utf-8")
    comment = " ".join(source.split("IDEMPOTENCY_OPTIONAL =", 1)[0][-2000:].split())
    assert f"Every write tool but the {words.get(len(required), len(required))} " \
           "below is # ``optional``" in comment


def test_the_reason_ceiling_is_the_models():
    """fee_payment.reverse_fee_payment sanitizes the motif at 500 — a
    longer one would be cut in silence."""
    import inspect

    assert "max_length=500" in inspect.getsource(fee_payment.reverse_fee_payment)
    assert tools.REVERSAL_REASON_MAX_CHARS == 500


# ══════════════════════════════════════════════════════════════════════
# 1. Le fidéicommis — une recette, sa compensation, un déboursé
# ══════════════════════════════════════════════════════════════════════


def test_a_deposit_is_recorded_en_circulation_by_the_connector(fake):
    payload = _call("record_trust_entry", **_deposit())
    _conforms("record_trust_entry", payload)
    entry = payload["entity"]
    stored = fake.peek(f"trust_transactions/{entry['id']}")
    assert stored["status"] == "en_circulation"
    assert stored["created_via"] == "mcp"
    assert entry["etag"] == stored["etag"]
    assert payload["entity_type"] == "trust_transaction"
    assert payload["client_balance"] == {
        "book_cents": 200000, "book_display": payload["client_balance"]["book_display"],
        "cleared_cents": 0, "cleared_display": payload["client_balance"]["cleared_display"]}
    assert payload["admin_recette"] is None and payload["invoice"] is None
    assert any("EN CIRCULATION" in w for w in payload["warnings"])


def test_a_disbursement_draws_only_on_cleared_funds(fake):
    _call("record_trust_entry", **_deposit())
    before = _entries(fake, "trust_transactions")
    refusal = _refused("record_trust_entry", **_deposit(
        50000, day="2026-09-04", direction="déboursé",
        purpose="déboursé_tiers", counterparty="Huissier X"))
    assert refusal.reason == "accounting_refused"
    assert _entries(fake, "trust_transactions") == before


def test_clearing_releases_the_funds_then_a_disbursement_passes(fake):
    rec = _call("record_trust_entry", **_deposit())
    cleared = _call("clear_register_entries", register="trust",
                    tx_ids=[rec["entity"]["id"]], cleared_date="2026-09-03")
    _conforms("clear_register_entries", cleared)
    assert cleared["count"] == 1
    assert cleared["released_funds"][0]["amount_cents"] == 200000
    stored = fake.peek(f"trust_transactions/{rec['entity']['id']}")
    assert stored["status"] == "compensée" and stored["cleared_via"] == "mcp"

    out = _call("record_trust_entry", **_deposit(
        50000, day="2026-09-04", direction="déboursé",
        purpose="déboursé_tiers", counterparty="Huissier X"))
    assert out["client_balance"]["cleared_cents"] == 150000


def test_clearing_refuses_what_the_statement_cannot_show(fake):
    rec = _call("record_trust_entry", **_deposit())
    tx = rec["entity"]["id"]
    assert "futur" in str(_refused("clear_register_entries", register="trust",
                                   tx_ids=[tx], cleared_date="2026-09-25"))
    assert "après la date du relevé" in str(_refused(
        "clear_register_entries", register="trust", tx_ids=[tx],
        cleared_date="2026-09-01"))
    assert "plus d'une fois" in str(_refused(
        "clear_register_entries", register="trust", tx_ids=[tx, tx],
        cleared_date="2026-09-03"))
    assert fake.peek(f"trust_transactions/{tx}")["status"] == "en_circulation"


def test_record_trust_entry_never_withdraws_cash_nor_pays_a_paper_or_provision_invoice(fake):
    """The « trust_withdrawal » promise (mcp/disclosure): no cash withdrawal
    (art. 57) — and the « fee_invoice » promise (split from it in lot 5,
    step 5): no fee payment on a paper invoice (no tool DECLARES
    invoice_external_ref, and the service call turns the external path
    off), none on an invoice that imputes a provision, none on an invoice
    not yet sent, and — decision D21, 2026-09-29 — none on another client's
    invoice — each refused, and nothing written anywhere."""
    _cleared_deposit(fake)
    before = {c: _entries(fake, c) for c in (
        "trust_transactions", "admin_transactions", "invoices")}

    cash = _refused("record_trust_entry", **_deposit(
        10000, day="2026-09-04", direction="déboursé",
        purpose="remise_client", method="comptant"))
    assert cash.reason == "accounting_refused" and "art. 57" in str(cash)

    props = tools.TOOLS["record_trust_entry"]["input_schema"]["properties"]
    assert "invoice_external_ref" not in props and "cash_receipt_id" not in props
    assert tools.validate_args(tools.TOOLS["record_trust_entry"]["input_schema"],
                               {**_fee(), "invoice_external_ref": "Papier-12",
                                "idempotency_key": _key()})
    import inspect

    source = inspect.getsource(handlers._record_trust_entry_impl)
    assert "allow_external_ref=False" in source

    provision = _refused("record_trust_entry", **_fee(20000, invoice_id="inv2"))
    assert provision.reason == "accounting_refused" and "provision" in str(provision)

    draft = _refused("record_trust_entry", **_fee(20000, invoice_id="inv3"))
    assert draft.reason == "accounting_refused" and "envoyée" in str(draft)

    by_cheque_to_a_third = _refused("record_trust_entry", **_fee(method="traite"))
    assert "art. 58" in str(by_cheque_to_a_third)

    other_client = _refused("record_trust_entry", **_fee(20000, client_id="c2"))
    assert other_client.reason == "accounting_refused"
    assert trust._ABORT_MESSAGES["facture_autre_client"] in str(other_client)

    assert {c: _entries(fake, c) for c in before} == before


def test_a_fee_payment_writes_its_three_effects_in_one_operation(fake):
    _cleared_deposit(fake)
    payload = _call("record_trust_entry", **_fee())
    _conforms("record_trust_entry", payload)
    assert payload["entity_type"] == "trust_fee_payment"
    trust_id = payload["entity"]["id"]
    recette = payload["admin_recette"]
    assert recette["trust_transaction_id"] == trust_id
    assert recette["invoice_id"] == "inv1"
    assert payload["invoice"]["status_before"] == "envoyée"
    assert payload["invoice"]["status_after"] == "payée"
    assert payload["invoice"]["paid_in_full"] is True
    invoice = fake.peek("invoices/inv1")
    assert invoice["amount_paid"] == 100000 and invoice["status"] == "payée"
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 100000
    # The payee art. 58 allows, when none is named: the firm — said.
    assert payload["entity"]["counterparty"] == "Poirier Lavoie, avocat"
    assert any("Bénéficiaire inscrit par défaut : « Poirier Lavoie, avocat »" in w
               for w in payload["warnings"])
    assert any("Rien n'a été envoyé au client" in w for w in payload["warnings"])


def test_d20_an_unsent_invoice_is_refused_without_a_way_around(fake):
    """D20 (2026-09-29, art. 56 2°): the check still trusts the « envoyée »
    status — the text alone changed. The refusal named the tool that
    promotes the invoice (« Promouvez-la d'abord (update_invoice) »): it
    told Claude how to get past the check. It now names the LAWYER as the
    one who attests the sending, in the model's own words, and nothing
    else."""
    _cleared_deposit(fake)
    before = {c: _entries(fake, c) for c in (
        "trust_transactions", "admin_transactions", "invoices")}
    draft = _refused("record_trust_entry", **_fee(20000, invoice_id="inv3"))
    assert draft.reason == "accounting_refused"
    text = str(draft)
    assert trust._ABORT_MESSAGES["facture_non_émise"] in text
    assert "envoyée par le juriste" in text and "art. 56 2°" in text
    for forbidden in ("update_invoice", "romouv", "marquez"):
        assert forbidden not in text, forbidden
    assert {c: _entries(fake, c) for c in before} == before


def test_d21_one_client_s_funds_never_settle_another_client_s_invoice(fake):
    """Régression — D21 (2026-09-29): inv1 is addressed to c1; drawing c2's
    CLEARED funds for it passed, with a warning after the fees had left
    trust. Refused now, no override — named by the handler, decided again
    by the model in the payment's transaction —, nothing written anywhere,
    and neither a name nor an amount in the refusal."""
    _cleared_deposit(fake)
    rec = _call("record_trust_entry", **_deposit(client_id="c2", day="2026-09-04"))
    _call("clear_register_entries", register="trust",
          tx_ids=[rec["entity"]["id"]], cleared_date="2026-09-04")
    before = {c: _entries(fake, c) for c in (
        "trust_transactions", "admin_transactions", "invoices")}
    refusal = _refused("record_trust_entry", **_fee(client_id="c2"))
    assert refusal.reason == "accounting_refused"
    assert trust._ABORT_MESSAGES["facture_autre_client"] in str(refusal)
    for word in ("Jean", "Marie", "Tremblay", "1 000"):
        assert word not in str(refusal), word
    assert {c: _entries(fake, c) for c in before} == before


def test_d21_the_model_decides_even_past_the_handler(fake, monkeypatch):
    """The handler's repetition is a courtesy: with its read made to see the
    invoice addressed to c2, the MODEL — which reads the invoice inside the
    payment's transaction — still refuses."""
    _cleared_deposit(fake)
    rec = _call("record_trust_entry", **_deposit(client_id="c2", day="2026-09-04"))
    _call("clear_register_entries", register="trust",
          tx_ids=[rec["entity"]["id"]], cleared_date="2026-09-04")
    real = svc.resolve_fee_invoice

    def _seen_as_c2(**kw):
        return {**real(**kw), "client_id": "c2"}

    monkeypatch.setattr(svc, "resolve_fee_invoice", _seen_as_c2)
    before = _entries(fake, "trust_transactions")
    refusal = _refused("record_trust_entry", **_fee(client_id="c2"))
    assert trust._ABORT_MESSAGES["facture_autre_client"] in str(refusal)
    assert _entries(fake, "trust_transactions") == before


def test_d23_the_payee_is_the_lawyer_or_his_firm(fake):
    """Régression — D23 (2026-09-29, art. 58): a caller-supplied payee went
    to the register as typed — « Jean Tremblay » passed. It must name the
    lawyer or his firm, as the firm profile names them; it is stored as the
    profile spells it; omitted, it is the firm."""
    _cleared_deposit(fake)
    refusal = _refused("record_trust_entry", **_fee(20000, counterparty="Jean Tremblay"))
    assert refusal.reason == "accounting_refused"
    text = str(refusal)
    assert "`counterparty`" in text and "art. 58" in text
    assert "« Poirier Lavoie, avocat » ou « Me Jason Poirier Lavoie »" in text
    assert not any(t.get("purpose") == "virement_honoraires"
                   for t in _entries(fake, "trust_transactions").values())

    payload = _call("record_trust_entry", **_fee(
        20000, counterparty="  me JASON poirier   lavoie "))
    assert payload["entity"]["counterparty"] == "Me Jason Poirier Lavoie"
    stored = fake.peek(f"trust_transactions/{payload['entity']['id']}")
    assert stored["counterparty"] == "Me Jason Poirier Lavoie"
    assert not any("Bénéficiaire inscrit" in w for w in payload["warnings"])


def test_d23_a_cheque_s_default_payee_is_the_lawyer(fake):
    """Régression — art. 58 draws a fee CHEQUE « à l'ordre de l'avocat » and
    names the société only as the holder of a TRANSFER's account. Omitted,
    the payee was the firm whatever the method: a cheque inscribed, by
    nobody's choice, to the firm's order. The default follows the method
    now — the lawyer on a chèque, the firm on a virement
    (test_a_fee_payment_writes_its_three_effects_in_one_operation) — and
    the warning says which. The firm NAMED on a cheque stays accepted
    (D23: the lawyer's choice)."""
    _cleared_deposit(fake)
    payload = _call("record_trust_entry", **_fee(20000, method="chèque"))
    assert payload["entity"]["counterparty"] == "Me Jason Poirier Lavoie"
    stored = fake.peek(f"trust_transactions/{payload['entity']['id']}")
    assert stored["counterparty"] == "Me Jason Poirier Lavoie"
    assert any("Bénéficiaire inscrit par défaut : « Me Jason Poirier Lavoie »" in w
               for w in payload["warnings"])
    named = _call("record_trust_entry", **_fee(
        20000, method="chèque", counterparty="Poirier Lavoie, avocat"))
    assert named["entity"]["counterparty"] == "Poirier Lavoie, avocat"
    assert not any("Bénéficiaire inscrit" in w for w in named["warnings"])


def test_d23_a_profile_that_names_no_one_refuses_every_fee_payment(fake):
    _cleared_deposit(fake)
    fake.seed("settings/cabinet", {"nom": "", "organisation": ""})
    refusal = _refused("record_trust_entry", **_fee(20000))
    assert refusal.reason == "accounting_refused"
    assert "ne nomme ni l'avocat ni le cabinet" in str(refusal)


def test_d23_an_unreadable_profile_refuses_rather_than_trust_the_seed(
    fake, monkeypatch
):
    """Régression — review of D23 (concurrency and atomicity): with
    ``settings/cabinet`` unreadable, the guard judged on the deploy-time
    SEED (``cabinet_dict`` fails open) — so a profile that no longer names
    the firm let « Poirier Lavoie, avocat », the seed's literal, go to the
    register as the payee. The handler's own list is still the fail-open
    display one (a courtesy); the MODEL reads strictly and refuses, nothing
    written, and the connector says so in the model's words. (Old code:
    the payment passed.)"""
    _cleared_deposit(fake)
    fake.seed("settings/cabinet", {"nom": "Me Jason Poirier Lavoie",
                                   "organisation": ""})
    server = fake._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kw):
        if any(d.endswith("/settings/cabinet") for d in request["documents"]):
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    before = {c: _entries(fake, c) for c in (
        "trust_transactions", "admin_transactions", "invoices")}
    refusal = _refused("record_trust_entry", **_fee(
        20000, counterparty="Poirier Lavoie, avocat"))
    assert refusal.reason == "accounting_refused"
    assert fee_payment._PROFILE_UNREADABLE in str(refusal)
    assert {c: _entries(fake, c) for c in before} == before


def test_d24_the_objet_sens_rule_is_the_model_s_too(fake):
    """D24 (2026-09-29): the handler's refusal of an objet that contradicts
    its sens is a REPETITION — the model refuses the same pair for every
    caller (the web form included), in its own words."""
    report = svc.enregistrer_ecriture_fideicommis({
        "account_id": "acc1", "direction": "déboursé", "amount": 1000,
        "purpose": "dépôt_client", "method": "chèque",
        "counterparty": "Jean Tremblay", "dossier_id": "dos1", "client_id": "c1",
        "date": _d(2026, 9, 4), "reference": "", "description": ""})
    assert not report["ok"] and report["reason"] == "objet_sens_incohérent"
    assert report["errors"] == [trust._ABORT_MESSAGES["objet_sens_incohérent"]]


def test_fee_only_arguments_are_refused_on_an_ordinary_entry(fake):
    refusal = _refused("record_trust_entry", **_deposit(
        invoice_id="inv1", admin_account_id="ops1"))
    assert "paiement d'honoraires" in str(refusal)
    missing = _refused("record_trust_entry", **{
        k: v for k, v in _fee().items() if k != "admin_account_id"})
    assert "admin_account_id" in str(missing)


def test_a_client_must_be_a_client_of_the_dossier(fake):
    assert "client de ce dossier" in str(_refused(
        "record_trust_entry", **_deposit(client_id="zz")))
    assert "vont ensemble" in str(_refused(
        "record_trust_entry", **{k: v for k, v in _deposit().items()
                                 if k != "client_id"}))


def test_bank_interest_carries_no_dossier_and_only_it_may(fake):
    payload = _call("record_trust_entry", account_id="acc1", direction="recette",
                    purpose="intérêts", amount_cents=125, date="2026-09-02",
                    method="dépôt_direct", counterparty="Desjardins")
    _conforms("record_trust_entry", payload)
    assert payload["entity"]["dossier_id"] == "" and payload["client_balance"] is None
    refusal = _refused("record_trust_entry", **{
        k: v for k, v in _deposit(day="2026-09-03").items()
        if k not in ("dossier_id", "client_id")})
    assert "seuls les intérêts et les frais bancaires" in str(refusal)


def test_an_objet_that_contradicts_its_direction_is_refused(fake):
    """Revue du lot 5b (argent et règlement) : le modèle ne lie pas l'objet
    au sens, et le connecteur inscrivait « Dépôt du client » en DÉBOURSÉ ou
    « Remise au client » en RECETTE — la ligne du registre de l'art. 38
    disait le contraire du mouvement. Les quatre objets dont le NOM tranche
    le sens sont refusés à contre-sens, sans rien écrire ; « règlement »
    va dans les deux sens."""
    _cleared_deposit(fake)
    before = _entries(fake, "trust_transactions")
    for purpose, direction in (("dépôt_client", "déboursé"),
                               ("avance_honoraires", "déboursé"),
                               ("remise_client", "recette"),
                               ("déboursé_tiers", "recette")):
        refusal = _refused("record_trust_entry", **_deposit(
            1000, day="2026-09-04", purpose=purpose, direction=direction,
            counterparty="Jean Tremblay"))
        assert refusal.reason == "accounting_refused", purpose
        assert "contraire du mouvement" in str(refusal), purpose
    assert _entries(fake, "trust_transactions") == before

    out = _call("record_trust_entry", **_deposit(
        1000, day="2026-09-04", purpose="règlement", direction="déboursé",
        counterparty="Me X, en fidéicommis"))
    assert (out["entity"]["purpose"], out["entity"]["direction"]) == (
        "règlement", "déboursé")
    out = _call("record_trust_entry", **_deposit(
        2000, day="2026-09-04", purpose="règlement", direction="recette"))
    assert out["entity"]["direction"] == "recette"


def test_a_disbursement_with_no_client_is_refused_for_what_it_is(fake):
    """Revue du lot 5b : un déboursé sans dossier ni client (des frais
    bancaires) ne passe JAMAIS le contrôle des fonds compensés — il se tient
    par client, et aucun client ne couvre celui-ci. Le refus disait « solde
    compensé insuffisant… attendez la compensation des dépôts » : une
    attente sans fin. Il dit maintenant pourquoi, et n'écrit rien."""
    _cleared_deposit(fake)
    before = _entries(fake, "trust_transactions")
    refusal = _refused("record_trust_entry", account_id="acc1",
                       direction="déboursé", purpose="frais_bancaires",
                       amount_cents=500, date="2026-09-04",
                       method="dépôt_direct", counterparty="Desjardins")
    assert refusal.reason == "accounting_refused"
    assert "jamais permis" in str(refusal)
    assert "Attendez la compensation" not in str(refusal)
    assert _entries(fake, "trust_transactions") == before
    # The model's own verdict, unchanged: the same entry is refused there.
    _entry, errors = trust.create_transaction({
        "account_id": "acc1", "direction": "déboursé",
        "purpose": "frais_bancaires", "amount": 500, "date": _d(2026, 9, 4),
        "method": "dépôt_direct", "counterparty": "Desjardins"})
    assert errors


def test_a_fee_payment_on_a_reconciled_admin_day_names_admin_date(fake):
    """Revue du lot 5b (D16) : le plancher de conciliation du compte
    d'opérations refuse la recette d'un paiement d'honoraires datée dans la
    période conciliée — et le refus du modèle nomme le champ du FORMULAIRE
    web. Au connecteur, il nomme l'argument, `admin_date` ; rien n'est
    écrit, et la date réelle du dépôt passe."""
    _cleared_deposit(fake)
    fake.seed("admin_reconciliations/rec1", {
        "id": "rec1", "account_id": "ops1", "status": "complétée",
        "period_end": _d(2026, 9, 12)})
    before = {c: _entries(fake, c) for c in (
        "trust_transactions", "admin_transactions", "invoices")}
    refusal = _refused("record_trust_entry", **_fee())
    assert refusal.reason == "accounting_refused"
    assert "`admin_date`" in str(refusal)
    assert {c: _entries(fake, c) for c in before} == before
    out = _call("record_trust_entry", **_fee(admin_date="2026-09-15"))
    assert out["admin_recette"]["date"] == "2026-09-15"


def test_a_future_or_backdated_entry_is_refused(fake):
    assert "futur" in str(_refused("record_trust_entry",
                                   **_deposit(day="2026-09-21")))
    _call("record_trust_entry", **_deposit(day="2026-09-10"))
    earlier = _refused("record_trust_entry", **_deposit(day="2026-09-02"))
    assert earlier.reason == "accounting_refused"


# ══════════════════════════════════════════════════════════════════════
# 2. Le registre d'administration
# ══════════════════════════════════════════════════════════════════════


def test_a_depense_is_split_by_the_ledger_never_guessed(fake):
    payload = _call("record_admin_entry", **_depense())
    _conforms("record_admin_entry", payload)
    net, tps, tvq = al.extract_taxes_from_gross(11498)
    e = payload["entity"]
    assert (e["net_amount_cents"], e["gst_amount_cents"], e["qst_amount_cents"]) == (
        net, tps, tvq)
    stored = fake.peek(f"admin_transactions/{e['id']}")
    assert stored["direction"] == "déboursé" and stored["created_via"] == "mcp"

    sans = _call("record_admin_entry", **_depense(5000, ventilation="sans_taxe"))
    assert sans["entity"]["net_amount_cents"] == 5000
    assert sans["entity"]["gst_amount_cents"] == 0

    assert "Une dépense exige `ventilation`" in str(_refused(
        "record_admin_entry", **{k: v for k, v in _depense().items()
                                 if k != "ventilation"}))
    assert "doivent égaler le montant" in str(_refused(
        "record_admin_entry", **_depense(ventilation="détaillée", net_cents=100,
                                         gst_cents=5, qst_cents=10)))
    assert "ne s'applique pas" in str(_refused(
        "record_admin_entry", **_depense(invoice_id="inv1")))


def test_an_encaissement_records_the_payment_in_the_same_operation(fake):
    payload = _call("record_admin_entry", account_id="ops1",
                    kind="encaissement_facture", amount_cents=30000,
                    date="2026-09-06", method="virement", invoice_id="inv1",
                    counterparty="Jean Tremblay")
    _conforms("record_admin_entry", payload)
    assert payload["invoice"]["status_before"] == "envoyée"
    assert payload["invoice"]["status_after"] == "envoyée"
    assert payload["invoice"]["balance_cents"] == 70000
    assert fake.peek("invoices/inv1")["amount_paid"] == 30000

    over = _refused("record_admin_entry", account_id="ops1",
                    kind="encaissement_facture", amount_cents=80000,
                    date="2026-09-06", method="virement", invoice_id="inv1",
                    counterparty="Jean Tremblay")
    assert over.reason == "accounting_refused" and "solde dû" in str(over)
    assert fake.peek("invoices/inv1")["amount_paid"] == 30000


def test_a_card_payment_writes_its_two_legs(fake):
    payload = _call("record_admin_entry", account_id="ops1",
                    kind="paiement_carte", amount_cents=25000,
                    date="2026-09-07", method="virement",
                    card_account_id="card1")
    _conforms("record_admin_entry", payload)
    assert payload["entity_type"] == "admin_card_payment"
    bank, card = payload["entity"], payload["card_leg"]
    assert bank["direction"] == "déboursé" and card["direction"] == "recette"
    assert bank["related_transaction_id"] == card["id"]
    assert "counterparty" not in tools.TOOLS["record_admin_entry"][
        "input_schema"]["required"]


def test_update_admin_entry_replaces_what_it_names_against_the_etag(fake):
    made = _call("record_admin_entry", **_depense())["entity"]
    payload = _call("update_admin_entry", tx_id=made["id"],
                    expected_etag=made["etag"], description="Loyer de septembre")
    _conforms("update_admin_entry", payload)
    stored = fake.peek(f"admin_transactions/{made['id']}")
    assert payload["outcome"] == "applied"
    assert stored["description"] == "Loyer de septembre"
    assert stored["updated_via"] == "mcp"
    assert payload["entity"]["etag"] == stored["etag"] != made["etag"]

    again = _call("update_admin_entry", tx_id=made["id"],
                  expected_etag=stored["etag"], description="Loyer de septembre")
    _conforms("update_admin_entry", again)
    assert again["outcome"] == "unchanged" and again["changed_fields"] == []
    assert fake.peek(f"admin_transactions/{made['id']}")["etag"] == stored["etag"]


def test_a_depense_amount_moves_only_with_its_ventilation(fake):
    made = _call("record_admin_entry", **_depense())["entity"]
    refusal = _refused("update_admin_entry", tx_id=made["id"],
                       expected_etag=made["etag"], amount_cents=20000)
    assert "ventilation" in str(refusal)
    ok = _call("update_admin_entry", tx_id=made["id"], expected_etag=made["etag"],
               amount_cents=20000, ventilation="sans_taxe")
    assert ok["entity"]["amount_cents"] == 20000
    assert ok["entity"]["net_amount_cents"] == 20000


def test_a_cleared_entry_is_no_longer_editable(fake):
    made = _call("record_admin_entry", **_depense())["entity"]
    _call("clear_register_entries", register="admin", tx_ids=[made["id"]],
          cleared_date="2026-09-08",
          expected_etags=_etags(fake, made["id"]))
    current = fake.peek(f"admin_transactions/{made['id']}")
    refusal = _refused("update_admin_entry", tx_id=made["id"],
                       expected_etag=current["etag"], description="x")
    assert refusal.reason == "accounting_refused"
    assert "reverse_register_entry" in str(refusal)


def test_an_admin_clearing_certifies_the_version_the_caller_read(fake):
    """Finitions, money-3 — the connector's half of the lot-5b « Compenser »
    fix. Claude reads a dépense of 114,98 $ (get_admin_ledger) to compare it
    with the statement; meanwhile the entry is corrected to 200,00 $. The
    clearing sent with the etag Claude READ is refused, whole, and nothing
    is locked: the 200,00 $ is never certified « on the statement » by a
    call that compared 114,98 $."""
    made = _call("record_admin_entry", **_depense())["entity"]
    (row,) = handlers.get_admin_ledger({"account_id": "ops1"})["transactions"]
    seen = row["etag"]
    assert row["amount_cents"] == 11498
    _call("update_admin_entry", tx_id=made["id"], expected_etag=seen,
          amount_cents=20000, ventilation="sans_taxe")
    before = _entries(fake, "admin_transactions")

    refusal = _refused("clear_register_entries", register="admin",
                       tx_ids=[made["id"]], cleared_date="2026-09-08",
                       expected_etags=[seen])

    assert refusal.reason == "stale_etag"
    assert made["id"] in str(refusal) and "Rien n'a été compensé" in str(refusal)
    assert "get_admin_ledger" in str(refusal)
    assert "20000" not in str(refusal) and "200,00" not in str(refusal)
    assert _entries(fake, "admin_transactions") == before
    assert fake.peek(f"admin_transactions/{made['id']}")["status"] == "en_circulation"
    # With the version read AFTER the correction, it clears.
    ok = _call("clear_register_entries", register="admin", tx_ids=[made["id"]],
               cleared_date="2026-09-08",
               expected_etags=_etags(fake, made["id"]))
    assert ok["entries"][0]["status"] == "compensée"
    assert ok["entries"][0]["amount_cents"] == 20000


def test_an_admin_clearing_is_refused_when_the_entry_moves_inside_the_call(
        fake, monkeypatch):
    """The model re-checks each version in its transaction: an edit landing
    between the handler's read and the commit refuses too — the handler's
    read agreeing with the caller proves nothing about the commit."""
    made = _call("record_admin_entry", **_depense())["entity"]
    seen = _etags(fake, made["id"])
    stale_copy = dict(fake.peek(f"admin_transactions/{made['id']}"))
    _call("update_admin_entry", tx_id=made["id"], expected_etag=seen[0],
          amount_cents=20000, ventilation="sans_taxe")
    # The handler's own read still sees the version the caller saw.
    real_read = svc.lire_ecriture
    monkeypatch.setattr(svc, "lire_ecriture", lambda register, tx_id: (
        stale_copy if tx_id == made["id"] else real_read(register, tx_id)))

    refusal = _refused("clear_register_entries", register="admin",
                       tx_ids=[made["id"]], cleared_date="2026-09-08",
                       expected_etags=seen)

    assert refusal.reason == "stale_etag"
    assert made["id"] in str(refusal)
    assert fake.peek(f"admin_transactions/{made['id']}")["status"] == "en_circulation"


def test_the_clearing_s_etags_are_required_at_admin_and_refused_at_trust(fake):
    made = _call("record_admin_entry", **_depense())["entity"]
    missing = _refused("clear_register_entries", register="admin",
                       tx_ids=[made["id"]], cleared_date="2026-09-08")
    assert "`expected_etags` est requis" in str(missing)
    other = _call("record_admin_entry", **_depense(500))["entity"]
    short = _refused("clear_register_entries", register="admin",
                     tx_ids=[made["id"], other["id"]], cleared_date="2026-09-08",
                     expected_etags=_etags(fake, made["id"]))
    assert "un etag par écriture" in str(short)
    rec = _call("record_trust_entry", **_deposit())["entity"]
    trust_refusal = _refused("clear_register_entries", register="trust",
                             tx_ids=[rec["id"]], cleared_date="2026-09-03",
                             expected_etags=[rec["etag"]])
    assert "ne vaut qu'au registre d'administration" in str(trust_refusal)
    for tx in (made["id"], other["id"]):
        assert fake.peek(f"admin_transactions/{tx}")["status"] == "en_circulation"
    assert fake.peek(f"trust_transactions/{rec['id']}")["status"] == "en_circulation"
    spec = tools.TOOLS["clear_register_entries"]
    assert "expected_etags" in spec["concurrency_reason"]
    assert "expected_etags" in spec["description"]


def test_the_train_s_pilot_moves_the_balance_as_deployment_says_and_locks_both_sides(fake):
    """Review of lot 5, step 5 (money lens). DEPLOYMENT.md §15 « Lot 5 »,
    step 6, is the lawyer's supervised pilot on a TEST administration
    account, and it now states the balance to expect after each call — a
    balance other than those is an écart, and the pilot stops. The recipe
    said « its four entries » (there are TWO: the dépense and its reversal)
    and let (d) « bring the account back to 0,00 $ » — the balance is ALREADY
    zero after (c), since ``admin_delta`` counts every status. This runs the
    four calls through the real handlers and pins each figure.

    It also pins the consent partial's editability clause against the
    model: « jamais contre-passée » named only the ORIGINAL of a pair, while
    the reversal itself (``reverses_id``, en circulation after a cleared
    original) is locked by ``_entry_lock_reason`` too — the partial now
    names both sides."""
    fake.seed("admin_accounts/essai", {
        "id": "essai", "name": "Essai — connecteur", "institution": "",
        "status": "actif", "account_type": "opérations", "transit": "",
        "account_number_last4": "", "ledger_balance": 0,
    })

    def balance() -> int:
        return fake.peek("admin_accounts/essai")["ledger_balance"]

    # (a) the dépense of 1,00 $, today, sans taxe.
    made = _call("record_admin_entry", **_depense(
        100, account_id="essai", date="2026-09-20", ventilation="sans_taxe",
        category="autre"))["entity"]
    assert made["status"] == "en_circulation" and balance() == -100
    stored = fake.peek(f"admin_transactions/{made['id']}")
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (100, 0, 0)
    # (b) cleared at today's date: the balance does not move.
    _call("clear_register_entries", register="admin", tx_ids=[made["id"]],
          cleared_date="2026-09-20",
          expected_etags=_etags(fake, made["id"]))
    assert fake.peek(f"admin_transactions/{made['id']}")["status"] == "compensée"
    assert balance() == -100
    # (c) reversed: the reversal enters en circulation — and the balance is
    # zero AT ONCE.
    rev = _call("reverse_register_entry", register="admin", tx_id=made["id"],
                reason="Pilote du connecteur")
    reversal_id = rev["entity"]["id"]
    assert rev["entity"]["status"] == "en_circulation"
    assert rev["original"]["status_after"] == "compensée"
    assert balance() == 0
    # The reversal, en circulation and linked to nothing else, is NOT
    # editable — the side « jamais contre-passée » did not name.
    current = fake.peek(f"admin_transactions/{reversal_id}")
    assert al._entry_lock_reason(current, None) == "écriture_verrouillée"
    before = _entries(fake, "admin_transactions")
    refusal = _refused("update_admin_entry", tx_id=reversal_id,
                       expected_etag=current["etag"], description="x")
    assert refusal.reason == "accounting_refused"
    assert _entries(fake, "admin_transactions") == before
    # (d) the reversal cleared: still zero, nothing outstanding — and the
    # account holds exactly TWO entries.
    _call("clear_register_entries", register="admin", tx_ids=[reversal_id],
          cleared_date="2026-09-20",
          expected_etags=_etags(fake, reversal_id))
    rows = [r for r in _entries(fake, "admin_transactions").values()
            if r.get("account_id") == "essai"]
    assert len(rows) == 2 and balance() == 0
    assert sum(al.admin_delta(r["direction"], r["amount"]) for r in rows) == 0
    assert {r["status"] for r in rows} == {"compensée"}

    deployment = (_ATHENA.parent / "DEPLOYMENT.md").read_text(encoding="utf-8")
    flat = " ".join(deployment.split())
    assert "its four entries" not in flat
    assert "its TWO entries (the dépense and its reversal)" in flat
    for figure in ("the account's balance reads **−1,00 $**",
                   "still **−1,00 $**", "the balance reads **0,00 $** AT ONCE",
                   "still **0,00 $**, now with nothing outstanding"):
        assert figure in flat, figure
    partial = " ".join((_ATHENA / "templates" / "mcp" / "families" /
                        "_comptabilite.html").read_text(encoding="utf-8").split())
    assert "ni contre-passée ni elle-même une contre-passation" in partial
    assert "et jamais contre-passée" not in partial


def test_nothing_is_dated_in_a_reconciled_period(fake):
    outstanding = _call("record_admin_entry", **_depense(date="2026-09-08"))
    fake.seed("admin_reconciliations/r1", {
        "id": "r1", "account_id": "ops1", "status": "complétée",
        "period_end": _d(2026, 9, 10)})
    made_after = _call("record_admin_entry", **_depense(date="2026-09-12"))
    assert made_after["entity"]["date"] == "2026-09-12"
    refused = _refused("record_admin_entry", **_depense(date="2026-09-09"))
    assert refused.reason == "accounting_refused"
    # An entry left outstanding by the reconciliation clears only AFTER it.
    tx = outstanding["entity"]["id"]
    in_period = _refused("clear_register_entries", register="admin",
                         tx_ids=[tx], cleared_date="2026-09-10",
                         expected_etags=_etags(fake, tx))
    assert in_period.reason == "accounting_refused"
    assert "conciliée" in str(in_period)
    assert fake.peek(f"admin_transactions/{tx}")["status"] == "en_circulation"
    ok = _call("clear_register_entries", register="admin", tx_ids=[tx],
               cleared_date="2026-09-11",
               expected_etags=_etags(fake, tx))
    assert ok["entries"][0]["status"] == "compensée"
    # …and its date is behind the lock now: no longer editable.
    (row,) = [r for r in handlers.get_admin_ledger({"account_id": "ops1"})[
        "transactions"] if r["id"] == tx]
    assert row["locked"] is True


# ══════════════════════════════════════════════════════════════════════
# 3. La contre-passation — la seule correction d'un registre
# ══════════════════════════════════════════════════════════════════════


def test_reversing_an_uncleared_trust_entry_voids_both(fake):
    rec = _call("record_trust_entry", **_deposit())
    payload = _call("reverse_register_entry", register="trust",
                    tx_id=rec["entity"]["id"], reason="Chèque sans provision")
    _conforms("reverse_register_entry", payload)
    assert payload["original"]["status_before"] == "en_circulation"
    assert payload["original"]["status_after"] == "annulée"
    assert fake.peek(f"trust_transactions/{rec['entity']['id']}")["status"] == "annulée"
    again = _refused("reverse_register_entry", register="trust",
                     tx_id=rec["entity"]["id"], reason="Encore")
    assert "déjà été contre-passée" in str(again)
    correction = _refused("reverse_register_entry", register="trust",
                          tx_id=payload["entity"]["id"], reason="x")
    assert "correction" in str(correction)
    assert "reversal_date" in str(_refused(
        "reverse_register_entry", register="trust", tx_id="t" * 8,
        reason="x", reversal_date="2026-09-10"))


def test_a_transfer_between_dossiers_is_never_reversed_here(fake):
    """Its reversal moves one client's funds back to another — the transfer
    the connector never makes (« register_transfer », split from
    « register_setup » in lot 5, step 5): refused before the model, nothing
    written."""
    fake.seed("trust_transactions/tv1", {
        "id": "tv1", "account_id": "acc1", "sequence": 1,
        "date": _d(2026, 9, 2), "direction": "déboursé", "amount": 1000,
        "purpose": "virement_inter_dossiers", "method": "virement",
        "status": "compensée", "dossier_id": "dos1", "client_id": "c1",
        "related_transaction_id": "tv2", "etag": "tv1-e0"})
    before = _entries(fake, "trust_transactions")
    refusal = _refused("reverse_register_entry", register="trust",
                       tx_id="tv1", reason="Erreur de dossier")
    assert refusal.reason == "accounting_refused"
    assert "dans l'application" in str(refusal)
    assert _entries(fake, "trust_transactions") == before


def test_a_fee_payment_reverses_with_its_recette_and_the_invoice_payment(fake):
    _cleared_deposit(fake)
    fee = _call("record_trust_entry", **_fee())
    recette_id = fee["admin_recette"]["id"]

    # Its recette reverses only from the trust side.
    refusal = _refused("reverse_register_entry", register="admin",
                       tx_id=recette_id, reason="Erreur")
    assert fee["entity"]["id"] in str(refusal)

    payload = _call("reverse_register_entry", register="trust",
                    tx_id=fee["entity"]["id"], reason="Facture erronée")
    _conforms("reverse_register_entry", payload)
    assert [r["admin_transaction_id"] for r in payload["admin_reversals"]] == [recette_id]
    (block,) = payload["invoices"]
    assert block["status_before"] == "payée" and block["status_after"] == "envoyée"
    invoice = fake.peek("invoices/inv1")
    assert invoice["amount_paid"] == 0 and invoice["status"] == "envoyée"
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 0


def test_an_admin_reversal_may_be_dated_between_the_original_and_today(fake):
    made = _call("record_admin_entry", **_depense())["entity"]
    assert "précède" in str(_refused(
        "reverse_register_entry", register="admin", tx_id=made["id"],
        reason="Doublon", reversal_date="2026-09-01"))
    payload = _call("reverse_register_entry", register="admin",
                    tx_id=made["id"], reason="Doublon",
                    reversal_date="2026-09-06")
    _conforms("reverse_register_entry", payload)
    assert payload["entity"]["date"] == "2026-09-06"
    assert payload["register"] == "admin"


# ══════════════════════════════════════════════════════════════════════
# 4. La lecture — get_admin_ledger
# ══════════════════════════════════════════════════════════════════════


def test_the_ledger_reads_newest_first_with_running_balances(fake):
    _call("record_admin_entry", account_id="ops1", kind="recette_autre",
          amount_cents=10000, date="2026-09-02", method="virement",
          counterparty="Remboursement")
    _call("record_admin_entry", **_depense(5000, ventilation="sans_taxe"))
    payload = handlers.get_admin_ledger({"account_id": "ops1"})
    _conforms("get_admin_ledger", payload)
    rows = payload["transactions"]
    assert [r["kind"] for r in rows] == ["dépense", "recette_autre"]
    assert [r["balance_after_cents"] for r in rows] == [5000, 10000]
    assert payload["opening_balance_cents"] == 0
    assert all(r["locked"] is False and r["lock_reason"] is None for r in rows)
    assert {a["id"] for a in payload["accounts"]} == {"ops1", "card1"}

    filtered = handlers.get_admin_ledger({"account_id": "ops1", "kind": "dépense"})
    _conforms("get_admin_ledger", filtered)
    assert [r["kind"] for r in filtered["transactions"]] == ["dépense"]
    assert filtered["transactions"][0]["balance_after_cents"] is None
    assert filtered["opening_balance_cents"] is None

    every = handlers.get_admin_ledger({})
    _conforms("get_admin_ledger", every)
    assert every["account_id"] is None
    assert all(r["balance_after_cents"] is None for r in every["transactions"])

    with pytest.raises(tools.ToolArgumentError, match="introuvable"):
        handlers.get_admin_ledger({"account_id": "nope"})


def test_a_locked_row_says_why(fake):
    made = _call("record_admin_entry", **_depense())["entity"]
    _call("clear_register_entries", register="admin", tx_ids=[made["id"]],
          cleared_date="2026-09-08",
          expected_etags=_etags(fake, made["id"]))
    (row,) = handlers.get_admin_ledger({"account_id": "ops1"})["transactions"]
    assert row["locked"] is True and row["lock_reason"] == "écriture_verrouillée"


def test_no_accounting_payload_ever_carries_a_transit_or_account_number(fake):
    """The « account_number » promise (mcp/disclosure): every accounting
    payload — the read, each write, each branch — carries neither the
    accounts' transit nor their account number, nor a receipt's storage
    path; the keys are never emitted either."""
    payloads = []
    rec = _call("record_trust_entry", **_deposit())
    payloads.append(rec)
    payloads.append(_call("clear_register_entries", register="trust",
                          tx_ids=[rec["entity"]["id"]], cleared_date="2026-09-03"))
    payloads.append(_call("record_trust_entry", **_fee()))
    dep = _call("record_admin_entry", **_depense())
    payloads.append(dep)
    fake.external_write(f"admin_transactions/{dep['entity']['id']}", {
        **fake.peek(f"admin_transactions/{dep['entity']['id']}"),
        "receipt_storage_path": RECEIPT_PATH})
    payloads.append(_call("record_admin_entry", account_id="ops1",
                          kind="paiement_carte", amount_cents=2500,
                          date="2026-09-07", method="virement",
                          card_account_id="card1"))
    payloads.append(handlers.get_admin_ledger({}))
    payloads.append(handlers.get_admin_ledger({"account_id": "ops1"}))
    # Review of lot 5, step 5 (money lens): the promise is shown to an
    # accounting token for the WHOLE connector, and the train (DEPLOYMENT.md
    # §15 « Lot 5 », step 5) checks get_trust_snapshot by hand — the two
    # trust reads carry the seeded account's transit and last 4 digits
    # neither, on the real store.
    payloads.append(handlers.get_trust_snapshot({}))
    payloads.append(handlers.list_trust_transactions({"account_id": "acc1"}))
    payloads.append(_call("reverse_register_entry", register="trust",
                          tx_id=payloads[2]["entity"]["id"], reason="Erreur"))
    payloads.append(_call("reverse_register_entry", register="admin",
                          tx_id=dep["entity"]["id"], reason="Doublon"))

    dumped = json.dumps(payloads, ensure_ascii=False, default=str)
    assert TRANSIT not in dumped and LAST4 not in dumped
    assert RECEIPT_PATH not in dumped and "administration/rx" not in dumped
    for forbidden in ("transit", "account_number", "last4",
                      "receipt_storage_path", "storage_path"):
        assert f'"{forbidden}' not in dumped, forbidden
    ledger = handlers.get_admin_ledger({"account_id": "ops1"})
    assert any(r["has_receipt"] for r in ledger["transactions"])
    # Non-vacuous: the trust reads DID read the seeded account and entries.
    snapshot = json.dumps(payloads[7], ensure_ascii=False, default=str)
    assert "Desjardins" in snapshot and "Général" in snapshot
    assert payloads[8]["transactions"]


# ══════════════════════════════════════════════════════════════════════
# 5. Le rejeu et l'issue incertaine
# ══════════════════════════════════════════════════════════════════════


def test_a_replay_records_nothing_and_vouches_for_no_before_state(fake):
    _cleared_deposit(fake)
    args = {**_fee(), "idempotency_key": "cle-rejeu-honoraires"}
    first = _call("record_trust_entry", **args)
    count = len(_entries(fake, "trust_transactions"))
    again = _call("record_trust_entry", **args)
    _conforms("record_trust_entry", again)
    assert again["idempotent_replay"] is True
    assert again["entity"]["id"] == first["entity"]["id"]
    assert again["invoice"]["status_before"] is None
    assert again["client_balance"] is None
    assert again["warnings"][0].startswith("Rejeu")
    assert len(_entries(fake, "trust_transactions")) == count


def test_a_reversal_replay_leaves_its_before_states_null(fake):
    rec = _call("record_trust_entry", **_deposit())
    args = {"register": "trust", "tx_id": rec["entity"]["id"],
            "reason": "Erreur", "idempotency_key": "cle-rejeu-contrepassation"}
    _call("reverse_register_entry", **args)
    again = _call("reverse_register_entry", **args)
    _conforms("reverse_register_entry", again)
    assert again["original"]["status_before"] is None
    assert again["client_cleared_after_cents"] is None


def _land_then(fake, monkeypatch, exc, marker: str) -> None:
    """The next commit touching *marker* APPLIES, then its caller is answered
    *exc* — a commit whose answer was lost."""
    server = fake._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(marker in server._write_name(w) for w in writes):
            state["armed"] = False
            raise exc
        return response

    monkeypatch.setattr(server, "commit", _commit)


def test_an_uncertain_outcome_keeps_the_claim_and_never_writes_twice(fake, monkeypatch):
    """The commit LANDED, its answer was lost: the call is refused as
    uncertain — never « nothing was written » — and its key stays claimed,
    so a same-key retry is refused rather than recording a second entry
    only a reversal could take back."""
    from google.api_core import exceptions as gexc

    _land_then(fake, monkeypatch, gexc.DeadlineExceeded("answer lost"),
               "/trust_transactions/")
    args = {**_deposit(), "idempotency_key": "cle-issue-incertaine"}
    first = _refused("record_trust_entry", **args)
    assert first.reason == "accounting_outcome_uncertain"
    assert first.keep_claim is True
    assert "Rien n'a été inscrit" not in str(first)
    assert len(_entries(fake, "trust_transactions")) == 1       # it DID land
    retry = _refused("record_trust_entry", **args)
    assert retry.reason in ("idempotency_in_flight", "idempotency_interrupted")
    assert len(_entries(fake, "trust_transactions")) == 1


@pytest.mark.parametrize("lost", ["deadline", "aborted_rerun"])
@pytest.mark.parametrize("case", [
    "trust_reversal", "admin_reversal", "trust_clear", "admin_clear",
    "admin_correction",
])
def test_a_lost_answer_never_applies_a_reversal_a_clearing_or_a_correction_twice(
    fake, monkeypatch, case, lost,
):
    """Review of lot 5, step 5 (concurrency lens). OBSERVABILITY.md says
    what the connector answers when a NON-create accounting write commits
    and its answer is lost: it cannot tell — only the creates (and the fee
    payment both ways) detect their own landed commit and keep the claim
    (``accounting_outcome_uncertain``). A reversal, a clearing or an
    administration correction then reads as a REFUSAL — the model's
    « Erreur lors de … » (``deadline``: the answer lost outright), or, when
    the SDK re-runs the transaction over the landed commit (``aborted_rerun``:
    the commit RPC retried and answered Aborted), its own guard — and the
    claim is RELEASED. That is only tenable because none of the three can
    apply twice: each re-checks, inside its transaction, the state it moves
    the entry FROM. Pinned here on the real store, through the real
    handlers: the write landed once, the answer was a refusal (never
    CommittedWriteError, never a « rien n'a été inscrit » claimed of a
    create), and the SAME-key retry is refused with nothing written again."""
    from google.api_core import exceptions as gexc

    if case in ("trust_reversal", "trust_clear"):
        target = _call("record_trust_entry", **_deposit())["entity"]
    else:
        target = _call("record_admin_entry", **_depense())["entity"]
    collections = ("trust_transactions", "admin_transactions", "trust_accounts",
                   "admin_accounts", "dossiers", "invoices")
    tool, args, marker = {
        "trust_reversal": ("reverse_register_entry",
                           {"register": "trust", "tx_id": target["id"],
                            "reason": "Doublon"}, "/trust_transactions/"),
        "admin_reversal": ("reverse_register_entry",
                           {"register": "admin", "tx_id": target["id"],
                            "reason": "Doublon"}, "/admin_transactions/"),
        "trust_clear": ("clear_register_entries",
                        {"register": "trust", "tx_ids": [target["id"]],
                         "cleared_date": "2026-09-03"}, "/trust_transactions/"),
        "admin_clear": ("clear_register_entries",
                        {"register": "admin", "tx_ids": [target["id"]],
                         "cleared_date": "2026-09-06",
                         "expected_etags": [target["etag"]]},
                        "/admin_transactions/"),
        "admin_correction": ("update_admin_entry",
                             {"tx_id": target["id"], "expected_etag": target["etag"],
                              "description": "Loyer de septembre"},
                             "/admin_transactions/"),
    }[case]
    args = {**args, "idempotency_key": f"cle-reponse-perdue-{case}-{lost}"}
    exc = (gexc.DeadlineExceeded("answer lost") if lost == "deadline"
           else gexc.Aborted("commit retried after a lost answer"))
    before = {c: _entries(fake, c) for c in collections}

    _land_then(fake, monkeypatch, exc, marker)
    first = _refused(tool, **args)
    assert first.keep_claim is False
    assert first.reason in ("accounting_refused", "stale_etag"), first.reason
    landed = {c: _entries(fake, c) for c in collections}
    assert landed != before                                     # it DID land, once
    stored = landed["trust_transactions" if case.startswith("trust")
                    else "admin_transactions"][target["id"]]
    if case.endswith("reversal"):
        assert stored["reversed_by_id"]
        rows = landed["trust_transactions" if case.startswith("trust")
                      else "admin_transactions"]
        assert sum(1 for r in rows.values() if r.get("reverses_id")) == 1
    elif case.endswith("clear"):
        assert stored["status"] == "compensée"
    else:
        assert stored["description"] == "Loyer de septembre"

    retry = _refused(tool, **args)
    assert retry.reason in ("accounting_refused", "stale_etag"), retry.reason
    assert {c: _entries(fake, c) for c in collections} == landed


def test_an_administration_clearing_that_errors_never_reads_as_a_verdict(
    fake, monkeypatch,
):
    """Review of lot 5, step 5 (concurrency lens). A Firestore exception in
    the administration clearing fell through to the VALIDATION refusal —
    « écriture déjà compensée ou annulée, date de compensation antérieure à
    l'écriture, ou future » — about an entry nothing had judged: the caller
    was sent to fix a date that was right. It now says what the trust twin
    says, « Erreur lors de la compensation », and nothing was written."""
    from google.api_core import exceptions as gexc

    made = _call("record_admin_entry", **_depense())["entity"]
    server = fake._fake_server
    real_commit = server.commit

    def _unavailable(request, metadata=None, **kwargs):
        # Only the register's commit fails — BEFORE anything applies (the
        # idempotency claim, another collection, commits normally).
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if any("/admin_transactions/" in server._write_name(w) for w in writes):
            raise gexc.InternalServerError("store down")
        return real_commit(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _unavailable)
    before = _entries(fake, "admin_transactions")
    refusal = _refused("clear_register_entries", register="admin",
                       tx_ids=[made["id"]], cleared_date="2026-09-06",
                       expected_etags=_etags(fake, made["id"]))
    assert "Erreur lors de la compensation" in str(refusal)
    assert "déjà compensée ou annulée" not in str(refusal)
    assert _entries(fake, "admin_transactions") == before
    report = svc.compenser_administration([made["id"]], _d(2026, 9, 6))
    assert report["reason"] == "compensation_erreur"
    assert report["errors"] == [al._ABORT_MESSAGES["compensation_erreur"]]


def test_an_administration_clearing_names_a_missing_account(fake):
    """The same fall-through named « déjà compensée … » for an entry whose
    account no longer exists — the abort's own reason is kept now."""
    made = _call("record_admin_entry", **_depense())["entity"]
    fake.external_delete("admin_accounts/ops1")
    report = svc.compenser_administration([made["id"]], _d(2026, 9, 6))
    assert report["ok"] is False and report["reason"] == "compte_introuvable"
    assert report["errors"] == [al._ABORT_MESSAGES["compte_introuvable"]]



def test_every_accounting_write_fails_closed_when_its_claim_cannot_be_read(
    fake, monkeypatch,
):
    """Plan rule 7: a money write never runs UNCLAIMED. With the idempotency
    store down, each of the five writes — through its REAL handler — is
    refused before anything reaches a register: an unclaimed run could be
    retried into a second entry that only a reversal takes back."""
    made = _call("record_admin_entry", **_depense())["entity"]
    deposit = _call("record_trust_entry", **_deposit())["entity"]
    registers = ("trust_transactions", "admin_transactions", "invoices",
                 "trust_accounts", "admin_accounts", "dossiers")
    before = {c: _entries(fake, c) for c in registers}
    broken = mock.Mock()
    broken.collection.side_effect = RuntimeError("firestore down")
    monkeypatch.setattr(write_support, "db", broken)
    calls = {
        "record_trust_entry": _deposit(),
        "record_admin_entry": _depense(),
        "update_admin_entry": {"tx_id": made["id"], "expected_etag": made["etag"],
                               "description": "Loyer"},
        "clear_register_entries": {"register": "trust", "tx_ids": [deposit["id"]],
                                   "cleared_date": "2026-09-03"},
        "reverse_register_entry": {"register": "admin", "tx_id": made["id"],
                                   "reason": "Doublon"},
    }
    assert set(calls) == set(tools.ACCOUNTING_WRITE_TOOLS)
    for tool, args in calls.items():
        refusal = _refused(tool, **args)
        assert refusal.reason == "idempotency_store_unavailable", tool
    assert {c: _entries(fake, c) for c in registers} == before


def test_a_failure_after_the_commit_says_recorded_and_never_writes_twice(
    fake, monkeypatch,
):
    """The commit point is structural (plan rule 7): every register writer
    notes its commit, so a failure AFTER it — here the payload builder —
    comes back as CommittedWriteError (« ENREGISTRÉE — NE PAS RÉESSAYER »),
    never a retryable internal error; and the same-key retry re-raises it
    from the stored partial without touching a register again."""
    made = _call("record_admin_entry", **_depense())["entity"]
    other = _call("record_admin_entry", **_depense(date="2026-09-06"))["entity"]
    deposit = _call("record_trust_entry", **_deposit())["entity"]
    calls = {
        "record_trust_entry": _deposit(5000, day="2026-09-05"),
        "record_admin_entry": _depense(date="2026-09-07"),
        "update_admin_entry": {"tx_id": made["id"], "expected_etag": made["etag"],
                               "description": "Loyer corrigé"},
        "clear_register_entries": {"register": "trust", "tx_ids": [deposit["id"]],
                                   "cleared_date": "2026-09-03"},
        "reverse_register_entry": {"register": "admin", "tx_id": other["id"],
                                   "reason": "Doublon"},
    }
    assert set(calls) == set(tools.ACCOUNTING_WRITE_TOOLS)
    registers = ("trust_transactions", "admin_transactions")
    for tool, args in calls.items():
        args = {**args, "idempotency_key": f"cle-apres-commit-{tool}"}
        before = {c: _entries(fake, c) for c in registers}
        with mock.patch.object(handlers, "_register_money",
                               side_effect=RuntimeError("builder down")):
            with pytest.raises(tools.CommittedWriteError) as first:
                _call(tool, **args)
        assert first.value.replay is False, tool
        written = {c: _entries(fake, c) for c in registers}
        assert written != before, tool                    # it DID commit
        with pytest.raises(tools.CommittedWriteError) as again:
            _call(tool, **args)
        assert again.value.replay is True, tool
        assert {c: _entries(fake, c) for c in registers} == written, tool

# ── Une compensation glissée entre la lecture et la contre-passation ─────
#
# Revue du lot 5b (concurrence). Le statut d'une écriture DÉCIDE ce que fait
# sa contre-passation : en circulation, les deux écritures deviennent
# annulée ; compensée, la contre-passation entre en circulation (un vrai
# mouvement à venir). Le gestionnaire lisait l'écriture, jugeait dessus et
# annonçait la transition lue — puis le modèle contre-passait l'écriture
# telle qu'elle était DEVENUE. Une compensation web (ou un appel parallèle)
# entre les deux : la réponse disait « annulée » à côté d'une contre-passation
# entrée en circulation. Le gestionnaire compare maintenant à SA lecture.


def _web_clear_right_after_the_handlers_read(monkeypatch, clear) -> list:
    """The handler's read of the entry returns, THEN a concurrent web request
    clears it — in its own thread, as a separate request is served (nested
    here, its commit would be handed up to this call's writing block and
    read as the connector's own)."""
    import threading

    real = svc.lire_ecriture
    fired: list = []

    def racing(register, tx_id):
        entry = real(register, tx_id)
        if not fired:
            fired.append(True)
            worker = threading.Thread(target=lambda: fired.append(clear()))
            worker.start()
            worker.join()
            assert fired[1]["ok"], fired[1]
        return entry

    monkeypatch.setattr(svc, "lire_ecriture", racing)
    return fired


def test_a_trust_reversal_raced_by_a_clearing_is_refused_never_misreported(
    fake, monkeypatch,
):
    rec = _call("record_trust_entry", **_deposit())
    tx_id = rec["entity"]["id"]
    fired = _web_clear_right_after_the_handlers_read(
        monkeypatch, lambda: svc.compenser_fideicommis([tx_id], _d(2026, 9, 3)))
    args = {"register": "trust", "tx_id": tx_id, "reason": "Doublon",
            "idempotency_key": "cle-course-contrepassation-fid"}

    refusal = _refused("reverse_register_entry", **args)
    assert fired and refusal.reason == "stale_etag"
    message = str(refusal)
    assert "Rien n'a été écrit" in message and "list_trust_transactions" in message
    assert "etag actuel" not in message          # its schema takes no etag
    stored = fake.peek(f"trust_transactions/{tx_id}")
    assert stored["status"] == "compensée"       # the clearing stands…
    assert not stored.get("reversed_by_id")      # …and nothing was reversed
    assert len(_entries(fake, "trust_transactions")) == 1

    # The refusal released the key: sent again, on a fresh read, the call
    # reverses the entry as it NOW is — and reports exactly that.
    payload = _call("reverse_register_entry", **args)
    _conforms("reverse_register_entry", payload)
    assert payload["idempotent_replay"] is False
    assert payload["original"] == {"id": tx_id, "status_before": "compensée",
                                   "status_after": "compensée"}
    assert payload["entity"]["status"] == "en_circulation"
    assert fake.peek(f"trust_transactions/{tx_id}")["status"] == "compensée"


def test_a_fee_payment_reversal_raced_by_a_clearing_writes_nowhere(
    fake, monkeypatch,
):
    _cleared_deposit(fake)
    fee = _call("record_trust_entry", **_fee())
    fee_id = fee["entity"]["id"]
    fired = _web_clear_right_after_the_handlers_read(
        monkeypatch, lambda: svc.compenser_fideicommis([fee_id], _d(2026, 9, 11)))

    refusal = _refused("reverse_register_entry", register="trust",
                       tx_id=fee_id, reason="Facture erronée")
    assert fired and refusal.reason == "stale_etag"
    trust_rows = _entries(fake, "trust_transactions")
    assert trust_rows[fee_id]["status"] == "compensée"
    assert not trust_rows[fee_id].get("reversed_by_id")
    assert not any(t.get("reverses_id") for t in trust_rows.values())
    assert not any(a.get("reverses_id") for a in
                   _entries(fake, "admin_transactions").values())
    invoice = fake.peek("invoices/inv1")
    assert invoice["amount_paid"] == 100000 and invoice["status"] == "payée"


def test_an_admin_reversal_raced_by_a_clearing_is_refused_never_misreported(
    fake, monkeypatch,
):
    made = _call("record_admin_entry", **_depense())["entity"]
    fired = _web_clear_right_after_the_handlers_read(
        monkeypatch,
        lambda: svc.compenser_administration([made["id"]], _d(2026, 9, 6)))
    args = {"register": "admin", "tx_id": made["id"], "reason": "Doublon",
            "idempotency_key": "cle-course-contrepassation-adm"}

    refusal = _refused("reverse_register_entry", **args)
    assert fired and refusal.reason == "stale_etag"
    assert "get_admin_ledger" in str(refusal)
    stored = fake.peek(f"admin_transactions/{made['id']}")
    assert stored["status"] == "compensée" and not stored.get("reversed_by_id")
    assert len(_entries(fake, "admin_transactions")) == 1

    payload = _call("reverse_register_entry", **args)
    _conforms("reverse_register_entry", payload)
    assert payload["original"] == {"id": made["id"], "status_before": "compensée",
                                   "status_after": "compensée"}
    assert payload["entity"]["status"] == "en_circulation"


def test_two_parallel_encaissements_through_the_tool_let_one_through(fake, monkeypatch):
    """Two calls, two keys, one invoice: the second call runs to completion
    right after the FIRST call's handler read the invoice (balance 1 000 $,
    both 600 $ fit). The handler's read is only a courtesy: the model reads
    the invoice again inside its transaction, and the first call is refused
    — exactly one payment recorded, the ledger and the invoice agreeing."""
    import contextvars

    real = svc.lire_facture
    fired: list = []

    def interleaved(invoice_id):
        invoice = real(invoice_id)
        if not fired:
            fired.append(True)
            # A separate request: a FRESH context, so its commit is never
            # handed up to this call's writing block (models.provenance).
            fired.append(contextvars.Context().run(
                _call, "record_admin_entry", account_id="ops1",
                kind="encaissement_facture", amount_cents=60000,
                date="2026-09-06", method="virement", invoice_id="inv1",
                counterparty="Jean Tremblay"))
        return invoice

    monkeypatch.setattr(svc, "lire_facture", interleaved)
    refusal = _refused("record_admin_entry", account_id="ops1",
                       kind="encaissement_facture", amount_cents=60000,
                       date="2026-09-06", method="virement", invoice_id="inv1",
                       counterparty="Jean Tremblay")
    assert fired and fired[1]["recorded"] is True
    assert refusal.reason == "accounting_refused" and "solde" in str(refusal)
    (entry,) = _entries(fake, "admin_transactions").values()
    assert entry["id"] == fired[1]["entity"]["id"]
    assert fake.peek("invoices/inv1")["amount_paid"] == 60000
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 60000

def test_an_edit_raced_by_a_clearing_names_the_lock_not_a_retry(fake, monkeypatch):
    """The edit read the entry editable; it was cleared before the model's
    transaction. « Redo it with its current etag » would lead straight into
    the lock refusal: the refusal says the entry is no longer editable and
    names the reversal — the web edit route's redirect, for the same case."""
    made = _call("record_admin_entry", **_depense())["entity"]
    fired = _web_clear_right_after_the_handlers_read(
        monkeypatch,
        lambda: svc.compenser_administration([made["id"]], _d(2026, 9, 6)))
    refusal = _refused("update_admin_entry", tx_id=made["id"],
                       expected_etag=made["etag"], description="Loyer")
    assert fired and refusal.reason == "stale_etag"
    message = str(refusal)
    assert "n'est plus modifiable" in message and "reverse_register_entry" in message
    assert "etag actuel" not in message
    stored = fake.peek(f"admin_transactions/{made['id']}")
    assert stored["status"] == "compensée" and stored["description"] != "Loyer"


# ══════════════════════════════════════════════════════════════════════
# 6. La portée — les registres ne sont atteints QUE par ces outils
# ══════════════════════════════════════════════════════════════════════

_REGISTER_MODULES = {"trust", "admin_ledger", "fee_payment"}
_WRITER_VERB = re.compile(
    r"^(create|update|set|record|append|void|reverse|clear|confirm|move|"
    r"delete|toggle|complete|attach|link|unlink|import|add|projeter|reduire)_")


def _index(source: str) -> tuple[dict, dict, dict]:
    tree = ast.parse(source)
    aliases, services, top = {}, {}, {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "models":
            for a in node.names:
                aliases[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module == "services":
            for a in node.names:
                services[a.asname or a.name] = a.name
        elif isinstance(node, ast.FunctionDef):
            top[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Name):
                    top[t.id] = node
    return top, aliases, services


def register_writers_reached(handlers_source: str, service_source,
                             registry: dict) -> dict[str, set]:
    """``{tool: {(register model, writer)}}`` — what each tool's handler
    reaches of the three register models, following the names it references
    in the handlers module and, through a ``from services import X as Y``
    binding, into that service's own closure. *service_source(name)* reads
    a service module (a test may plant one)."""
    indexes = {"": _index(handlers_source)}

    def index(module: str):
        if module not in indexes:
            indexes[module] = _index(service_source(module))
        return indexes[module]

    def reach(module: str, start: str, seen: set) -> set:
        top, aliases, services = index(module)
        stack, found = [start], set()
        while stack:
            name = stack.pop()
            if (module, name) in seen or name not in top:
                continue
            seen.add((module, name))
            for sub in ast.walk(top[name]):
                if isinstance(sub, ast.Name) and sub.id in top:
                    stack.append(sub.id)
                elif (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                      and sub.value.id in aliases
                      and aliases[sub.value.id] in _REGISTER_MODULES
                      and _WRITER_VERB.match(sub.attr)):
                    found.add((aliases[sub.value.id], sub.attr))
                elif (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                      and sub.value.id in services):
                    found |= reach(services[sub.value.id], sub.attr, seen)
        return found

    return {t: reach("", spec["handler"], set()) for t, spec in registry.items()}


def _service_source(name: str) -> str:
    return (_ATHENA / "services" / f"{name}.py").read_text(encoding="utf-8")


def test_only_the_accounting_tools_reach_a_register_writer():
    """The « trust » promise (mcp/disclosure): without the accounting grant
    the connector never touches the trust register nor the administration
    ledger. Backed by derivation: the tools whose handler reaches a register
    WRITER — directly, or through services/comptabilite — are exactly the
    accounting writes, every one of which demands athena:comptabilite; the
    accounting READ reaches none."""
    source = pathlib.Path(handlers.__file__).read_text(encoding="utf-8")
    reached = register_writers_reached(source, _service_source, tools.TOOLS)
    writers = {t for t, found in reached.items() if found}
    assert writers == set(tools.ACCOUNTING_WRITE_TOOLS), sorted(writers)
    assert reached["get_admin_ledger"] == set()
    for tool in writers:
        assert tools.required_scope(tool) == "athena:comptabilite", tool
    # Non-vacuous: the walker sees the composite and the plain paths.
    assert {("fee_payment", "create_fee_payment"),
            ("trust", "create_transaction")} <= reached["record_trust_entry"]
    assert ("fee_payment", "reverse_fee_payment") in reached["reverse_register_entry"]
    assert ("admin_ledger", "update_transaction") in reached["update_admin_entry"]


def test_the_register_walker_sees_a_planted_writer():
    """The walker bites: a tool that reaches a register writer — through a
    handler helper, a service, or a service's helper — is reported."""
    planted_handlers = (
        "from models import trust as trust_model\n"
        "from services import relay as r\n"
        "def read_tool(a):\n    return helper(a)\n"
        "def helper(a):\n    return r.go(a)\n"
        "def direct(a):\n    return trust_model.reverse_transaction(a)\n"
        "def clean(a):\n    return trust_model.list_transactions(a)\n"
    )
    planted_service = (
        "from models import admin_ledger as al\n"
        "def go(a):\n    return _inner(a)\n"
        "def _inner(a):\n    return al.clear_transactions_bulk(a)\n"
    )
    registry = {"t1": {"handler": "read_tool"}, "t2": {"handler": "direct"},
                "t3": {"handler": "clean"}}
    reached = register_writers_reached(
        planted_handlers, lambda n: planted_service, registry)
    assert reached == {"t1": {("admin_ledger", "clear_transactions_bulk")},
                       "t2": {("trust", "reverse_transaction")}, "t3": set()}


def test_no_connector_module_writes_a_payment_itself():
    """The payment-writer sweep: no mcp/ module — nor the accounting
    service it reaches — calls a payment writer. A payment is written by
    the REGISTER, inside the entry's own transaction (models/admin_ledger,
    models/fee_payment), never by a connector call beside it."""
    names = {"record_payment", "payment_updates", "projeter_paiement",
             "reduire_paiement"}
    paths = sorted((_ATHENA / "mcp").glob("*.py")) + [
        _ATHENA / "services" / "comptabilite.py"]
    offenders = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in names:
                    offenders.append((path.name, node.lineno, name))
    assert offenders == []


# ══════════════════════════════════════════════════════════════════════
# 7. Les preuves de la carte des formulaires web (TRANSITIONS)
# ══════════════════════════════════════════════════════════════════════


def _route_calls(route_file: str, fn_name: str) -> set[str]:
    tree = ast.parse((_ATHENA / "routes" / route_file).read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    return {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)}


def test_a_stale_web_transition_never_undoes_the_connectors(fake):
    """The web's clearing and reversal go through the SAME service calls
    (routes/trust), and the model decides each on the entry as it NOW is: a
    page read before the connector's clearing or reversal is refused, and
    the connector's write stands."""
    assert "comptabilite.compenser_fideicommis" in _route_calls("trust.py", "entry_clear")
    assert "comptabilite.contrepasser_ecriture_fideicommis" in _route_calls(
        "trust.py", "entry_reverse")

    rec = _call("record_trust_entry", **_deposit())
    _call("clear_register_entries", register="trust",
          tx_ids=[rec["entity"]["id"]], cleared_date="2026-09-03")
    stored = fake.peek(f"trust_transactions/{rec['entity']['id']}")
    web = svc.compenser_fideicommis([rec["entity"]["id"]], _d(2026, 9, 4))
    assert web["ok"] is False
    assert fake.peek(f"trust_transactions/{rec['entity']['id']}") == stored

    other = _call("record_trust_entry", **_deposit(5000, day="2026-09-05"))
    _call("reverse_register_entry", register="trust",
          tx_id=other["entity"]["id"], reason="Erreur")
    before = _entries(fake, "trust_transactions")
    web = svc.contrepasser_ecriture_fideicommis(other["entity"]["id"], "Page ouverte")
    assert web["ok"] is False
    assert _entries(fake, "trust_transactions") == before


def test_a_stale_web_reversal_of_a_fee_payment_the_connector_reversed_is_refused(fake):
    _cleared_deposit(fake)
    fee = _call("record_trust_entry", **_fee())
    _call("reverse_register_entry", register="trust",
          tx_id=fee["entity"]["id"], reason="Facture erronée")
    snapshot = {c: _entries(fake, c) for c in (
        "trust_transactions", "admin_transactions", "invoices")}
    web = svc.contrepasser_ecriture_fideicommis(fee["entity"]["id"], "Page ouverte")
    assert web["ok"] is False
    assert {c: _entries(fake, c) for c in snapshot} == snapshot
    assert fake.peek("invoices/inv1")["amount_paid"] == 0


# ══════════════════════════════════════════════════════════════════════
# 8. Les contrats d'outputSchema — le VRAI gestionnaire, chaque branche
# ══════════════════════════════════════════════════════════════════════


def test_the_trust_writes_honour_their_output_contract(fake):
    rec = handlers.record_trust_entry({**_deposit(), "idempotency_key": _key()})
    _conforms("record_trust_entry", rec)
    cleared = handlers.clear_register_entries({
        "register": "trust", "tx_ids": [rec["entity"]["id"]],
        "cleared_date": "2026-09-03", "idempotency_key": _key()})
    _conforms("clear_register_entries", cleared)
    fee_args = {**_fee(), "idempotency_key": _key()}
    fee = handlers.record_trust_entry(fee_args)
    _conforms("record_trust_entry", fee)
    replay = handlers.record_trust_entry(fee_args)
    _conforms("record_trust_entry", replay)
    assert replay["idempotent_replay"] is True
    reversed_fee = handlers.reverse_register_entry({
        "register": "trust", "tx_id": fee["entity"]["id"],
        "reason": "Facture erronée", "idempotency_key": _key()})
    _conforms("reverse_register_entry", reversed_fee)
    assert reversed_fee["register"] == "trust" and reversed_fee["invoices"]


def test_the_admin_writes_honour_their_output_contract(fake):
    dep = handlers.record_admin_entry({**_depense(), "idempotency_key": _key()})
    _conforms("record_admin_entry", dep)
    enc = handlers.record_admin_entry({
        "account_id": "ops1", "kind": "encaissement_facture",
        "amount_cents": 10000, "date": "2026-09-06", "method": "virement",
        "invoice_id": "inv1", "counterparty": "Jean Tremblay",
        "already_cleared_date": "2026-09-07", "idempotency_key": _key()})
    _conforms("record_admin_entry", enc)
    assert enc["entity"]["status"] == "compensée" and enc["invoice"]
    card = handlers.record_admin_entry({
        "account_id": "ops1", "kind": "paiement_carte", "amount_cents": 2500,
        "date": "2026-09-07", "method": "virement", "card_account_id": "card1",
        "idempotency_key": _key()})
    _conforms("record_admin_entry", card)
    assert card["card_leg"] is not None
    edited = handlers.update_admin_entry({
        "tx_id": dep["entity"]["id"], "expected_etag": dep["entity"]["etag"],
        "description": "Loyer", "idempotency_key": _key()})
    _conforms("update_admin_entry", edited)
    unchanged = handlers.update_admin_entry({
        "tx_id": dep["entity"]["id"], "expected_etag": edited["entity"]["etag"],
        "description": "Loyer", "idempotency_key": _key()})
    _conforms("update_admin_entry", unchanged)
    assert unchanged["outcome"] == "unchanged"
    cleared = handlers.clear_register_entries({
        "register": "admin", "tx_ids": [dep["entity"]["id"]],
        "cleared_date": "2026-09-08",
        "expected_etags": [unchanged["entity"]["etag"]],
        "idempotency_key": _key()})
    _conforms("clear_register_entries", cleared)
    assert cleared["released_funds"] == []
    rev_args = {"register": "admin", "tx_id": enc["entity"]["id"],
                "reason": "Encaissement en double", "idempotency_key": _key()}
    rev = handlers.reverse_register_entry(rev_args)
    _conforms("reverse_register_entry", rev)
    assert rev["register"] == "admin" and rev["invoices"]
    replay = handlers.reverse_register_entry(rev_args)
    _conforms("reverse_register_entry", replay)
    assert replay["original"]["status_before"] is None


# ══════════════════════════════════════════════════════════════════════
# 9. Les contrôles d'intégrité lisent ce que le connecteur écrit
# ══════════════════════════════════════════════════════════════════════


def test_the_integrity_scripts_agree_with_what_the_connector_wrote(fake, monkeypatch, capsys):
    """Revue du lot 5b (argent) : le train d'armement exige les deux
    contrôles d'intégrité PROPRES avant et après les pilotes
    (DEPLOYMENT.md §15). Une journée complète écrite par le connecteur —
    dépôt, compensation, paiement d'honoraires puis sa contre-passation, un
    second paiement, dépense ventilée corrigée puis compensée, autre
    recette, encaissement contre-passé, paiement de carte — et les deux
    scripts, lus sur le MÊME magasin, n'y trouvent rien : les soldes
    dénormalisés (compte, client, grand livre), la liaison D-4 de chaque
    paiement d'honoraires et le cumul encaissé de chaque facture
    concordent avec les écritures."""
    dep = _cleared_deposit(fake, 300000)
    fee = _call("record_trust_entry", **_fee(40000))
    _call("record_trust_entry", **_fee(60000, date="2026-09-11",
                                       admin_date="2026-09-12"))
    _call("record_trust_entry", **_deposit(
        50000, day="2026-09-12", direction="déboursé",
        purpose="déboursé_tiers", counterparty="Huissier X"))
    # A trust reversal is dated TODAY: it comes last (the backdating guard).
    _call("reverse_register_entry", register="trust",
          tx_id=fee["entity"]["id"], reason="Montant erroné")
    depense = _call("record_admin_entry", **_depense(11498))["entity"]
    fixed = _call("update_admin_entry", tx_id=depense["id"],
                  expected_etag=depense["etag"], amount_cents=22996,
                  ventilation="ventiler")["entity"]
    _call("clear_register_entries", register="admin", tx_ids=[fixed["id"]],
          cleared_date="2026-09-13",
          expected_etags=_etags(fake, fixed["id"]))
    _call("record_admin_entry", account_id="ops1", kind="recette_autre",
          amount_cents=1500, date="2026-09-13", method="virement",
          counterparty="Remboursement")
    enc = _call("record_admin_entry", account_id="ops1",
                kind="encaissement_facture", amount_cents=20000,
                date="2026-09-14", method="virement", invoice_id="inv1",
                counterparty="Jean Tremblay")
    _call("reverse_register_entry", register="admin",
          tx_id=enc["entity"]["id"], reason="Encaissement en double")
    _call("record_admin_entry", account_id="ops1", kind="paiement_carte",
          amount_cents=5000, date="2026-09-15", method="virement",
          card_account_id="card1")
    assert fake.peek("invoices/inv1")["amount_paid"] == 60000
    assert al.sum_invoice_receipts("inv1") == 60000
    assert dep

    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti

    install(monkeypatch, *_fake_modules(), vti, vai, fake=fake)
    for script in (vti, vai):
        code = script.main()
        out = capsys.readouterr().out
        assert code == 0, out


def test_the_trust_journal_shows_back_what_the_trust_writes_record(fake):
    """Finitions, contracts-3. record_trust_entry and clear_register_entries
    name list_trust_transactions as their read; its rows carried neither the
    cheque number, nor the description, nor the account, nor the invoice a
    fee payment settled — Claude could never read back what it wrote, nor
    match a statement line by cheque number, nor tell two accounts apart."""
    deposit = _call("record_trust_entry", **_deposit(
        reference="CHQ-1042", description="Provision pour frais"))["entity"]
    _cleared_deposit(fake)
    fee = _call("record_trust_entry", **_fee())["entity"]
    listing = handlers.list_trust_transactions({"account_id": "acc1"})
    _conforms("list_trust_transactions", listing)
    rows = {r["id"]: r for r in listing["transactions"]}
    row = rows[deposit["id"]]
    assert row["reference"] == "CHQ-1042"
    assert row["description"] == "Provision pour frais"
    assert row["account_id"] == "acc1"
    assert (row["dossier_id"], row["client_id"]) == ("dos1", "c1")
    assert row["created_via"] == "mcp"
    assert rows[fee["id"]]["invoice_id"] == "inv1"
    # The same names, the same values as the write's own entity.
    for key in ("reference", "description", "account_id", "created_via"):
        assert row[key] == deposit[key], key
    # Still never a bank number.
    flat = json.dumps(listing, ensure_ascii=False)
    assert TRANSIT not in flat and LAST4 not in flat
    # The filtered (window) shape carries them too.
    carte = handlers.list_trust_transactions({"dossier_id": "dos1",
                                              "client_id": "c1"})
    _conforms("list_trust_transactions", carte)
    assert {r["reference"] for r in carte["transactions"]} >= {"CHQ-1042"}


def test_the_new_trust_row_keys_are_optional_in_the_schema():
    """Added to an existing contract: never required (a replay stored or a
    client pinned before them)."""
    from mcp.output_schemas import OUTPUT_SCHEMAS
    item = OUTPUT_SCHEMAS["list_trust_transactions"]["properties"][
        "transactions"]["items"]
    for key in handlers._TRUST_LIST_EXTRA:
        assert key in item["properties"], key
        assert key not in item.get("required", []), key
