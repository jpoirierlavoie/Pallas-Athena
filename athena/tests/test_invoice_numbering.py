"""Year-sequential invoice numbering (models/invoice.py).

An invoice number is « YYYY-FNNN » again — the MONTRÉAL calendar year, the
« F » marker, and a 3-digit zero-padded year-wide sequence (user decision
2026-08-12, reverting the per-file « {file_number}-NN » scheme of
2026-07-17 after four weeks; the six invoices that scheme minted keep
their numbers for ever — an accounting artifact sent to a client is never
renumbered).

Same import-stub approach as test_trust: stub whatever google/firebase lib is
missing on a bare interpreter, and (in the integration fixture) patch
invoice.firestore so @firestore.transactional is an identity decorator —
otherwise the real decorator would drive the fake Transaction and fail.
"""

import importlib
import importlib.util
import sys
import types
from datetime import date
from unittest import mock

import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _avail(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _stub(name, module):
    parts = name.split(".")
    for i in range(1, len(parts)):
        pkg = ".".join(parts[:i])
        if pkg not in sys.modules:
            if _avail(pkg):
                importlib.import_module(pkg)
                continue
            m = types.ModuleType(pkg)
            m.__path__ = []
            sys.modules[pkg] = m
            if i > 1:
                setattr(sys.modules[".".join(parts[: i - 1])], parts[i - 1], m)
    sys.modules[name] = module
    if len(parts) > 1:
        setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)


if not _avail("google.cloud.firestore"):
    _fs = types.ModuleType("google.cloud.firestore")
    _fs.Client = mock.MagicMock(name="firestore.Client")
    _fs.Query = type("Query", (), {"ASCENDING": "ASCENDING", "DESCENDING": "DESCENDING"})
    _fs.Transaction = type("Transaction", (), {})
    _fs.transactional = lambda fn: fn
    _stub("google.cloud.firestore", _fs)
if not _avail("google.cloud.firestore_v1.base_query"):
    _bq = types.ModuleType("google.cloud.firestore_v1.base_query")
    _bq.FieldFilter = type(
        "FieldFilter", (), {"__init__": lambda s, field_path=None, op_string=None, value=None, **k: None}
    )
    _stub("google.cloud.firestore_v1.base_query", _bq)
if not _avail("icalendar"):
    _stub("icalendar", types.ModuleType("icalendar"))
if not _avail("firebase_admin"):
    _fa = types.ModuleType("firebase_admin")
    _fa.__path__ = []
    _stub("firebase_admin", _fa)
    _stub("firebase_admin.auth", types.ModuleType("firebase_admin.auth"))


with mock.patch("google.cloud.firestore.Client"):
    import models.invoice as invoice


# ── Fake Firestore (transactional counter + collection scan) ───────────────


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Query:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll
        self._filters = []

    def where(self, filter=None):
        self._filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def stream(self, transaction=None):
        rows = list(self._store.get(self._coll, {}).values())
        for fp, _op, val in self._filters:
            rows = [d for d in rows if d.get(fp) == val]
        return [_Snap(d.get("id"), d) for d in rows]


class _DocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def get(self, transaction=None):
        return _Snap(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = dict(data)


class _Coll(_Query):
    def document(self, doc_id):
        return _DocRef(self._store, self._coll, doc_id)


class _Txn:
    def __init__(self, store):
        self._store = store

    def set(self, ref, data):
        ref.set(data)


class _DB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _Coll(self._store, name)

    def transaction(self):
        return _Txn(self._store)


class _FS:
    transactional = staticmethod(lambda fn: fn)
    Transaction = object

    class Query:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"


class _FF:
    def __init__(self, field_path=None, op_string=None, value=None, **_k):
        self.field_path = field_path
        self.op_string = op_string
        self.value = value


@pytest.fixture
def store(monkeypatch):
    s = {"invoices": {}, "counters": {}}
    monkeypatch.setattr(invoice, "db", _DB(s))
    monkeypatch.setattr(invoice, "firestore", _FS)
    monkeypatch.setattr(invoice, "FieldFilter", _FF)
    # Millésime FIGÉ (jamais un offset dérivé de l'horloge — la leçon du
    # test de retard du 2026-08-11) : le générateur lit today_mtl, pas
    # datetime.now, et ce gel est ce que pinne test_millesime_de_montreal.
    monkeypatch.setattr(invoice, "today_mtl", lambda: date(2026, 6, 15))
    return s


# ── Le générateur annuel ───────────────────────────────────────────────────


def test_sequence_annuelle_monotone(store):
    assert invoice._generate_invoice_number() == "2026-F001"
    assert invoice._generate_invoice_number() == "2026-F002"
    assert invoice._generate_invoice_number() == "2026-F003"


def test_compteur_existant_continue_sans_reamorcage(store):
    # Le portrait de production du retour (2026-08-12) : compteur à 30 —
    # le prochain numéro est F031, jamais une réutilisation (un numéro
    # alloué puis brûlé ou supprimé reste un trou pour toujours).
    store["counters"]["invoices-2026"] = {"seq": 30}
    assert invoice._generate_invoice_number() == "2026-F031"


def test_amorcage_ignore_les_numeros_par_dossier(store):
    # Premier usage d'une année SANS compteur : l'amorçage balaie les
    # factures existantes — les « YYYY-FNNN » comptent, les numéros par
    # dossier de la parenthèse 2026-07-17→2026-08-12 sont invisibles au
    # préfixe « 2026-F » et ne peuvent ni collisionner ni décaler la suite.
    store["invoices"] = {
        "i1": {"id": "i1", "invoice_number": "2026-F007"},
        "i2": {"id": "i2", "invoice_number": "2026-F012"},
        "i3": {"id": "i3", "invoice_number": "2026-001-05"},
        "i4": {"id": "i4", "invoice_number": "2026-028-02"},
    }
    assert invoice._generate_invoice_number() == "2026-F013"


def test_amorcage_sans_facture_annuelle_demarre_a_f001(store):
    store["invoices"] = {
        "i1": {"id": "i1", "invoice_number": "2026-001-05"},
    }
    assert invoice._generate_invoice_number() == "2026-F001"


def test_millesime_de_montreal(store):
    # Le préfixe suit le jour civil de MONTRÉAL (today_mtl — l'unique
    # horloge maison), plus l'année UTC : une facture du 31 décembre au
    # soir porte le millésime en cours. Pinné en pointant today_mtl sur
    # une autre année que celle de l'horloge murale.
    invoice.today_mtl = lambda: date(2030, 12, 31)
    assert invoice._generate_invoice_number() == "2030-F001"


def test_debordement_a_quatre_chiffres_apres_f999(store):
    store["counters"]["invoices-2026"] = {"seq": 999}
    assert invoice._generate_invoice_number() == "2026-F1000"


def test_scan_max_invoice_seq_tolere_les_suffixes_non_numeriques(store):
    store["invoices"] = {
        "i1": {"id": "i1", "invoice_number": "2026-F009"},
        "i2": {"id": "i2", "invoice_number": "2026-Fxx"},   # jamais émis par nous
        "i3": {"id": "i3", "invoice_number": ""},
    }
    assert invoice._scan_max_invoice_seq("2026-F") == 9
