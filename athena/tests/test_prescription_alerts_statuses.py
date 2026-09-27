"""Alertes de prescription : un dossier « en attente » alerte encore (lot 0b).

Le défaut : ``list_prescription_alerts`` interrogeait ``status == actif``
SEULEMENT. Mettre un dossier en attente — un état du flux de travail, pas
une fermeture — taisait son délai de prescription au tableau de bord ET
dans le breffage MCP (``get_agenda``), sans aucune erreur. En droit, la
prescription court quel que soit l'état du dossier.

La réparation garde la forme de requête que l'index sert déjà — une
requête ``status == <s>`` PAR statut sur (status, prescription_date),
jamais un ``in`` + ``order_by`` — et fusionne en Python. Ces tests passent
par le VRAI client Firestore (``tests/_fake_firestore.py`` : seul le
serveur est faux).
"""

import logging
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
    from models import dossier as dmod

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
NOW = datetime.now(UTC).replace(microsecond=0)
CUTOFF = NOW + timedelta(days=60)


def _dossier(did: str, status: str, days: int, **over) -> dict:
    doc = {
        "id": did, "file_number": f"2026-{did}", "title": f"Dossier {did}",
        "status": status, "prescription_type": "3_ans",
        "prescription_date": NOW + timedelta(days=days),
        "clients": [], "opposing_parties": [],
    }
    doc.update(over)
    return doc


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, dmod)


def _seed(fake, *docs: dict) -> None:
    for d in docs:
        fake.seed(f"dossiers/{d['id']}", d)


def test_an_en_attente_dossier_still_alerts(fake):
    """THE defect: on the old code this dossier was never read."""
    _seed(fake, _dossier("a1", "en_attente", 10))
    alerts = dmod.list_prescription_alerts(CUTOFF)
    assert [a["id"] for a in alerts] == ["a1"]
    assert alerts[0]["prescription_status"] == "courante"


def test_open_statuses_alert_and_closed_ones_do_not(fake):
    _seed(
        fake,
        _dossier("act", "actif", 20),
        _dossier("att", "en_attente", 5),
        _dossier("fer", "fermé", 3),
        _dossier("arc", "archivé", 4),
    )
    ids = [a["id"] for a in dmod.list_prescription_alerts(CUTOFF)]
    assert sorted(ids) == ["act", "att"]


def test_the_two_statuses_merge_into_one_chronology(fake):
    """Each query is ordered on its own; the merge must restore ONE order
    (oldest raw date first) — the dashboard and the briefing read it."""
    _seed(
        fake,
        _dossier("a30", "actif", 30),
        _dossier("w10", "en_attente", 10),
        _dossier("a5", "actif", 5),
        _dossier("w40", "en_attente", 40),
    )
    ids = [a["id"] for a in dmod.list_prescription_alerts(CUTOFF)]
    assert ids == ["a5", "w10", "a30", "w40"]


def test_the_derivation_still_silences_an_interrupted_en_attente_dossier(fake):
    """The derive_prescription filter is unchanged, and applies to the new
    status too: a depot (or the legacy prise_action_date) silences."""
    _seed(
        fake,
        _dossier("att", "en_attente", 10,
                 prise_action_date=NOW - timedelta(days=2)),
        _dossier("act", "actif", 12),
    )
    assert [a["id"] for a in dmod.list_prescription_alerts(CUTOFF)] == ["act"]


def test_the_window_full_warning_is_judged_per_query(fake, caplog):
    """A full « actif » window must be said — and must not hide the
    « en_attente » alerts, which have their own window."""
    _seed(
        fake,
        *[_dossier(f"a{i}", "actif", i + 1) for i in range(3)],
        _dossier("w1", "en_attente", 50),
    )
    with caplog.at_level(logging.WARNING, logger=dmod.logger.name):
        alerts = dmod.list_prescription_alerts(CUTOFF, limit=3)
    ids = [a["id"] for a in alerts]
    assert "w1" in ids and len(ids) == 4
    full = [r.getMessage() for r in caplog.records
            if "result window full" in r.getMessage()]
    assert len(full) == 1 and "status=actif" in full[0]


def test_one_failed_status_query_never_hides_the_other(fake, monkeypatch):
    _seed(fake, _dossier("act", "actif", 10), _dossier("att", "en_attente", 5))
    real_collection = fake.collection

    class _Exploding:
        def __init__(self, inner):
            self._inner = inner

        def where(self, *a, filter=None, **k):  # noqa: A002 — SDK keyword
            if filter is not None and getattr(filter, "value", None) == "en_attente":
                raise RuntimeError("lecture impossible")
            return self._inner.where(*a, filter=filter, **k)

    monkeypatch.setattr(
        fake, "collection", lambda name: _Exploding(real_collection(name))
    )
    assert [a["id"] for a in dmod.list_prescription_alerts(CUTOFF)] == ["act"]


def test_a_dossier_read_by_both_queries_alerts_once(monkeypatch):
    """The two reads are not one snapshot: a dossier whose status flips
    between them comes back twice. It must alert once."""
    doc = _dossier("flip", "actif", 10)

    class _Snap:
        def to_dict(self):
            return dict(doc)

    query = mock.Mock()
    query.where.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.stream.return_value = [_Snap()]
    monkeypatch.setattr(dmod, "db", mock.Mock(collection=lambda n: query))
    assert [a["id"] for a in dmod.list_prescription_alerts(CUTOFF)] == ["flip"]


def test_every_query_is_one_status_equality_on_the_existing_index(monkeypatch):
    """No ``in`` + ``order_by`` (a shape the index does not serve would 400
    until an index builds, and the view would read EMPTY): one ``==`` per
    status, ordered on prescription_date — the (status, prescription_date)
    index."""
    filters: list[tuple[str, str, object]] = []
    orders: list[str] = []
    query = mock.Mock()

    def _where(filter=None):  # noqa: A002 — SDK keyword
        filters.append((filter.field_path, filter.op_string, filter.value))
        return query

    query.where.side_effect = _where
    query.order_by.side_effect = lambda field: orders.append(field) or query
    query.limit.return_value = query
    query.stream.return_value = []
    monkeypatch.setattr(dmod, "db", mock.Mock(collection=lambda n: query))

    dmod.list_prescription_alerts(CUTOFF)

    status_filters = [f for f in filters if f[0] == "status"]
    assert status_filters == [
        ("status", "==", s) for s in dmod.PRESCRIPTION_ALERT_STATUSES
    ]
    assert set(dmod.PRESCRIPTION_ALERT_STATUSES) == {"actif", "en_attente"}
    assert all(op != "in" for _, op, _ in filters)
    assert orders == ["prescription_date"] * 2


def test_a_row_the_derivation_cannot_read_alerts_unverified(fake, monkeypatch):
    """Never a silent drop: one unreadable row is alerted « a_verifier »
    on its raw date, and the others survive it (the old enclosing try
    emptied the whole list)."""
    _seed(fake, _dossier("bad", "actif", 10), _dossier("ok", "en_attente", 20))
    real = dmod.derive_prescription

    def _derive(doc):
        if doc.get("id") == "bad":
            raise ValueError("événement illisible")
        return real(doc)

    monkeypatch.setattr(dmod, "derive_prescription", _derive)
    by_id = {a["id"]: a for a in dmod.list_prescription_alerts(CUTOFF)}
    assert set(by_id) == {"bad", "ok"}
    assert by_id["bad"]["prescription_status"] == "a_verifier"
    assert by_id["bad"]["prescription_date_effective"] is None
