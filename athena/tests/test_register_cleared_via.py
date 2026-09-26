"""WHO cleared a register entry, beside WHEN (plan rule 5, lot 0a).

« Trust/admin rows gain created_via/cleared_via. » `created_via` arrived
with the provenance stamp (`provenance.create_fields`); `cleared_via` did
not, and `updated_via` cannot stand in for it: a later write to the entry
(a reversal marking it `annulée`, a reconciliation link) overwrites
`updated_via`, so the only record of whether a clearing was the lawyer's —
or, from plan lot 5, the connector's — would be gone. It is written by the
three paths that clear an entry, in both registers: the entry created
already `compensée`, the single/bulk clear, and a completed
reconciliation's ticked entries. `""` on an uncleared entry.

Run on the shared fake Firestore (the real client, transactions included),
and read back from what is STORED.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger
    from models import provenance
    from models import trust

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
_DAY = datetime(2026, 9, 1, tzinfo=UTC)
_REGISTERS = {"trust": trust, "admin": admin_ledger}


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, trust, admin_ledger)


def _seed(db, module, *, status="en_circulation"):
    db.seed(f"{module.ACCOUNTS_COLLECTION}/a1", {
        "id": "a1", "status": "actif", "account_type": "opérations",
        "book_balance": 0, "bank_balance": 0, "ledger_balance": 0,
        "etag": "acc0", "created_at": _DAY, "updated_at": _DAY,
    })
    db.seed(f"{module.TRANSACTIONS_COLLECTION}/t1", {
        "id": "t1", "account_id": "a1", "sequence": 1, "date": _DAY,
        "direction": "déboursé", "amount": 1000, "status": status,
        "cleared_date": None, "dossier_id": None, "client_id": None,
        "etag": "tx0", "created_at": _DAY, "updated_at": _DAY,
    })
    return f"{module.TRANSACTIONS_COLLECTION}/t1"


@pytest.mark.parametrize("register", sorted(_REGISTERS))
@pytest.mark.parametrize("via", ["web", "mcp"])
def test_a_clearing_records_who_cleared(db, register, via):
    module = _REGISTERS[register]
    path = _seed(db, module)
    with provenance.writing_via(via):
        entry, errors = module.clear_transaction("t1", _DAY + timedelta(days=1))
    assert errors == [], errors
    stored = db.peek(path)
    assert stored["status"] == "compensée"
    assert stored["cleared_via"] == via
    assert entry["cleared_via"] == via


@pytest.mark.parametrize("register", sorted(_REGISTERS))
def test_cleared_via_survives_a_later_write_that_updated_via_does_not(
    db, register,
):
    """The reason for a field of its own: the NEXT write rewrites
    `updated_via`; `cleared_via` still names the clearing's writer."""
    module = _REGISTERS[register]
    path = _seed(db, module)
    with provenance.writing_via("mcp"):
        module.clear_transaction("t1", _DAY + timedelta(days=1))
    later = db.peek(path)
    later.update(provenance.update_fields(datetime.now(UTC)))   # a script
    db.external_write(path, later)
    stored = db.peek(path)
    assert stored["updated_via"] == "script"
    assert stored["cleared_via"] == "mcp"


@pytest.mark.parametrize("register", sorted(_REGISTERS))
def test_every_clearing_path_of_the_model_stamps_cleared_via(register):
    """Swept on the source: every statement writing `"status": "compensée"`
    also writes `cleared_via` — so a fourth clearing path cannot land
    without it."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_REGISTERS[register]))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        values = {k.value: v for k, v in zip(node.keys, node.values)
                  if isinstance(k, ast.Constant)}
        clears = (isinstance(values.get("status"), ast.Constant)
                  and values["status"].value == "compensée")
        builds = "cleared_date" in keys and "id" in keys   # the row builder
        if (clears or builds) and "cleared_via" not in keys:
            offenders.append(node.lineno)
    assert offenders == []


@pytest.mark.parametrize("register", sorted(_REGISTERS))
def test_an_uncleared_row_is_built_with_an_empty_cleared_via(register):
    import inspect

    module = _REGISTERS[register]
    params = inspect.signature(module._build_transaction_doc).parameters
    kwargs = {name: None for name, p in params.items()
              if p.default is inspect.Parameter.empty}
    kwargs.update(tx_id="t9", account_id="a1", sequence=1,
                  direction="déboursé", amount=1000,
                  now=datetime.now(UTC))
    kwargs.update({k: _DAY for k in ("date", "date_value") if k in params})
    kwargs.update({k: "" for k in ("purpose", "method", "counterparty",
                                   "reference", "description", "kind",
                                   "supplier_invoice_ref", "invoice_number")
                   if k in params})
    if "ventilation" in params:
        kwargs["ventilation"] = {"net_amount": 1000, "gst_amount": 0,
                                 "qst_amount": 0}
    for key in ("balance_after_account", "balance_after_client"):
        if key in params:
            kwargs[key] = 0
    uncleared = module._build_transaction_doc(**kwargs)
    assert uncleared["cleared_via"] == ""
    with provenance.writing_via("web"):
        cleared = module._build_transaction_doc(
            **kwargs, status="compensée", cleared_date=_DAY)
    assert cleared["cleared_via"] == "web"
