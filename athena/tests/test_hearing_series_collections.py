"""Séries — la suppression tombstone chaque occurrence dans SA collection
(lot 0b, B4).

``delete_series`` prenait la collection DAV de la PREMIÈRE ligne seulement.
Une occurrence rattachée à un autre dossier — le formulaire web et le PUT DAV
le permettent aujourd'hui — vit dans une autre collection : elle était
supprimée de Firestore sans pierre tombale là où le téléphone la voit, et y
restait pour toujours (le modèle de synchro n'a pas d'autre signal de
retrait, et le document n'existe plus pour rejouer).

Épinglé contre le vrai client Firestore (``tests/_fake_firestore.py``) :
UN commit, une pierre tombale par occurrence dans la bonne collection, un
bump par collection touchée, et le budget du lot compté en 2N + K.
"""

import os
import sys
from datetime import date, datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import models.hearing as h

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, h, dav_sync)
    for name in ("dossier:d1", "dossier:d2", "general"):
        fake.seed(f"dav_sync/{name}", {"ctag": f"c0-{name}",
                                       "sync_token": f"c0-{name}"})
    return fake


def _series(count=4) -> list[dict]:
    occ, errors = h.create_hearing_series({
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
        "title": "Rencontre hebdomadaire", "hearing_type": "rencontre",
        "start_datetime": datetime(2026, 10, 6, 13, tzinfo=UTC),
        "end_datetime": datetime(2026, 10, 6, 14, tzinfo=UTC),
        "status": "confirmée",
    }, "hebdomadaire", count=count)
    assert errors == []
    return occ


def _move(hid: str, dossier_id: str) -> None:
    labels = {"d2": ("2026-002", "Roy c. Gagnon"), "": ("", "")}[dossier_id]
    doc, errors = h.update_hearing(hid, {
        "dossier_id": dossier_id, "dossier_file_number": labels[0],
        "dossier_title": labels[1]})
    assert errors == []


def _tombstones(fake, name: str) -> set[str]:
    return set(fake.peek_collection(f"dav_sync/{name}/tombstones"))


def test_each_occurrence_is_tombstoned_in_its_own_collection(fake):
    """THE defect: the occurrences moved to d2 and to « Général » were
    deleted from Firestore but tombstoned in dossier:d1, a collection the
    phone never reads them from."""
    occ = _series(4)
    _move(occ[1]["id"], "d2")
    _move(occ[2]["id"], "")

    rows, errors = h.delete_series(occ[0]["serie_id"])
    assert errors == [] and len(rows) == 4
    assert fake.peek_collection("hearings") == {}
    assert _tombstones(fake, "dossier:d1") == {occ[0]["id"], occ[3]["id"]}
    assert _tombstones(fake, "dossier:d2") == {occ[1]["id"]}
    assert _tombstones(fake, "general") == {occ[2]["id"]}


def test_every_collection_touched_is_bumped_and_its_tombstones_carry_the_new_token(
    fake,
):
    occ = _series(3)
    _move(occ[1]["id"], "d2")
    before = {n: fake.peek(f"dav_sync/{n}")["ctag"]
              for n in ("dossier:d1", "dossier:d2", "general")}

    h.delete_series(occ[0]["serie_id"])

    for name in ("dossier:d1", "dossier:d2"):
        state = fake.peek(f"dav_sync/{name}")
        assert state["ctag"] != before[name], name
        for tomb in fake.peek_collection(
                f"dav_sync/{name}/tombstones").values():
            assert tomb["sync_token"] == state["ctag"]
    # A collection no occurrence lived in is left alone.
    assert fake.peek("dav_sync/general")["ctag"] == before["general"]


def test_a_split_chain_is_still_deleted_in_one_atomic_commit(fake):
    """Two commits would not be atomic: a failure of the second would
    leave deletions without their tombstones."""
    occ = _series(4)
    _move(occ[1]["id"], "d2")
    _move(occ[2]["id"], "")
    fake.reset_logs()

    h.delete_series(occ[0]["serie_id"])

    commits = fake.commits
    assert len(commits) == 1, commits


def test_the_batch_budget_counts_one_bump_per_collection(fake, monkeypatch):
    """2N + K operations, not 2N + 1: with the chunk at 9, a four-occurrence
    chain spread over two collections (10 operations) must be refused
    before anything is written — the old count (9) let it through."""
    occ = _series(4)
    _move(occ[1]["id"], "d2")
    monkeypatch.setattr(dav_sync, "_BATCH_CHUNK", 9)
    rows, errors = h.delete_series(occ[0]["serie_id"])
    assert rows == [] and errors == [
        "Cette série est trop longue pour être supprimée d'un seul bloc."]
    assert len(fake.peek_collection("hearings")) == 4


def test_from_date_still_protects_past_occurrences_across_collections(fake):
    occ = _series(4)
    _move(occ[0]["id"], "d2")
    _move(occ[3]["id"], "d2")
    rows, errors = h.delete_series(occ[0]["serie_id"],
                                   from_date=date(2026, 10, 20))
    assert errors == []
    assert {r["id"] for r in rows} == {occ[2]["id"], occ[3]["id"]}
    assert set(fake.peek_collection("hearings")) == {occ[0]["id"],
                                                    occ[1]["id"]}
    assert _tombstones(fake, "dossier:d1") == {occ[2]["id"]}
    assert _tombstones(fake, "dossier:d2") == {occ[3]["id"]}


def test_the_sync_report_of_each_collection_announces_its_deletion(fake):
    """What DavX5 actually reads: get_tombstones per collection."""
    occ = _series(2)
    _move(occ[1]["id"], "d2")
    h.delete_series(occ[0]["serie_id"])
    assert {t["id"] for t in dav_sync.get_tombstones("dossier:d2")} \
        == {occ[1]["id"]}
    assert {t["id"] for t in dav_sync.get_tombstones("dossier:d1")} \
        == {occ[0]["id"]}
