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
    monkeypatch.setattr(handlers, "cabinet_dict", lambda: {
        "organisation": "Poirier Lavoie, avocat", "nom": "Me Jason Poirier Lavoie"})
    f = install(monkeypatch, *_fake_modules())
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
    (art. 57), no fee payment on a paper invoice (no tool DECLARES
    invoice_external_ref, and the service call turns the external path
    off), none on an invoice that imputes a provision — each refused, and
    nothing written anywhere."""
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
    # The payee art. 58 allows, when none is named: the firm.
    assert payload["entity"]["counterparty"] == "Poirier Lavoie, avocat"
    assert any("Rien n'a été envoyé au client" in w for w in payload["warnings"])


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
          cleared_date="2026-09-08")
    current = fake.peek(f"admin_transactions/{made['id']}")
    refusal = _refused("update_admin_entry", tx_id=made["id"],
                       expected_etag=current["etag"], description="x")
    assert refusal.reason == "accounting_refused"
    assert "reverse_register_entry" in str(refusal)


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
                         tx_ids=[tx], cleared_date="2026-09-10")
    assert in_period.reason == "accounting_refused"
    assert "conciliée" in str(in_period)
    assert fake.peek(f"admin_transactions/{tx}")["status"] == "en_circulation"
    ok = _call("clear_register_entries", register="admin", tx_ids=[tx],
               cleared_date="2026-09-11")
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
    the connector never makes (« register_setup »): refused before the
    model, nothing written."""
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
          cleared_date="2026-09-08")
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
        "cleared_date": "2026-09-08", "idempotency_key": _key()})
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
