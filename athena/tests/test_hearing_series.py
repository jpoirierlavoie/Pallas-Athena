"""Séries récurrentes d'audiences — models/hearing.

Pins the silent-failure zones a materialised series introduces:

- ``serie_id == ""`` is a STORED VALUE, not a sentinel: an equality query on
  it matches every standalone hearing in the practice, and « Détacher » is
  what puts a page in that state. Both the model guard and the shape of the
  query are pinned here.
- a caller-supplied ``id`` would make N ``batch.set()`` calls hit ONE document
  reference — Firestore keeps the last, silently, and N−1 occurrences vanish
  with a success return.
- the CTag bump rides INSIDE the batch: committing the documents and then
  bumping leaves the occurrences live in the web UI while DavX5 never
  re-syncs them.
- a timed series holds its Montréal wall clock across a DST switch.
- a chain action never touches a past occurrence.
"""

import copy
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.hearing as h
    import dav.sync as sync

from tz import to_mtl  # noqa: E402
from utils import recurrence  # noqa: E402

UTC = timezone.utc


# ── Fake Firestore, batch-aware ─────────────────────────────────────────
class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, store, coll, doc_id):
        self._store, self._coll, self.id = store, coll, doc_id

    def get(self, transaction=None):
        return _Snap(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = copy.deepcopy(data)

    def delete(self):
        self._store.setdefault(self._coll, {}).pop(self.id, None)

    def collection(self, name):
        return _CollRef(self._store, f"{self._coll}/{self.id}/{name}")


class _Query:
    def __init__(self, store, coll):
        self._store, self._coll = store, coll
        self._filters = []

    def where(self, filter=None):
        self._filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def order_by(self, field, direction="ASCENDING"):
        return self

    def limit(self, n):
        return self

    def stream(self):
        for doc_id, data in self._store.get(self._coll, {}).items():
            if all(data.get(fp) == val for fp, op, val in self._filters
                   if op == "=="):
                yield _Snap(doc_id, data)


class _CollRef(_Query):
    def document(self, doc_id):
        return _DocRef(self._store, self._coll, doc_id)


class _Batch:
    """Records every staged op so a test can assert ONE atomic commit."""

    def __init__(self, store, log):
        self._store, self._log, self._ops = store, log, []
        self.committed = False

    def set(self, ref, data):
        self._ops.append(("set", ref._coll, ref.id))

    def delete(self, ref):
        self._ops.append(("delete", ref._coll, ref.id))

    def commit(self):
        self.committed = True
        for kind, coll, doc_id in self._ops:
            ref = _DocRef(self._store, coll, doc_id)
            if kind == "delete":
                ref.delete()
        self._log.append(list(self._ops))


class _DB:
    def __init__(self, store, log):
        self._store, self._log = store, log

    def collection(self, name):
        return _CollRef(self._store, name)

    def batch(self):
        return _Batch(self._store, self._log)


class _FF:
    def __init__(self, field_path=None, op_string=None, value=None, **_kw):
        self.field_path, self.op_string, self.value = field_path, op_string, value


@pytest.fixture
def store():
    return {}


@pytest.fixture
def commits():
    return []


@pytest.fixture(autouse=True)
def _wire(monkeypatch, store, commits):
    """Both modules bind ``db`` at import, so both need patching — and the
    batch has to WRITE through so the staged sets land in the store."""
    db = _DB(store, commits)

    def _batch_set(self, ref, data):
        self._ops.append(("set", ref._coll, ref.id))
        ref.set(data)

    monkeypatch.setattr(_Batch, "set", _batch_set)
    monkeypatch.setattr(h, "db", db)
    monkeypatch.setattr(sync, "db", db)
    monkeypatch.setattr(h, "FieldFilter", _FF)


# ── Fixtures ────────────────────────────────────────────────────────────
def _proto(**over):
    """Un prototype horodaté : 9 h, heure de Montréal, le 15 septembre 2026."""
    base = {
        "dossier_id": "d1",
        "title": "Rencontre hebdomadaire",
        "hearing_type": "rencontre",
        "start_datetime": datetime(2026, 9, 15, 13, 0, tzinfo=UTC),  # 9 h MTL
        "end_datetime": datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        "all_day": False,
        "status": "confirmée",
    }
    base.update(over)
    return base


def _allday(**over):
    base = {
        "dossier_id": "d1",
        "title": "Bloc de rédaction",
        "hearing_type": "rencontre",
        "start_datetime": datetime(2026, 9, 15, tzinfo=UTC),
        "end_datetime": datetime(2026, 9, 15, 1, 0, tzinfo=UTC),
        "all_day": True,
    }
    base.update(over)
    return base


# ── Création ────────────────────────────────────────────────────────────
def test_creates_one_document_per_occurrence(store):
    occ, errors = h.create_hearing_series(_proto(), "hebdomadaire", count=4)
    assert errors == []
    assert len(occ) == 4
    assert len(store["hearings"]) == 4


def test_every_occurrence_gets_its_own_id_and_uid(store):
    """Le piège des 59 sur 60 : des batch.set() sur une même référence."""
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=6)
    assert len({o["id"] for o in occ}) == 6
    assert len({o["vevent_uid"] for o in occ}) == 6
    assert len({o["etag"] for o in occ}) == 6


def test_a_caller_supplied_id_is_stripped(store):
    """create_hearing HONORE un id fourni (l'affordance CalDAV). Le laisser
    passer ici écraserait toutes les occurrences sur un seul document."""
    occ, errors = h.create_hearing_series(
        _proto(id="fixe", vevent_uid="fixe-uid"), "hebdomadaire", count=5
    )
    assert errors == []
    assert len(store["hearings"]) == 5
    assert "fixe" not in store["hearings"]
    assert all(o["vevent_uid"] != "fixe-uid" for o in occ)


def test_all_occurrences_share_the_serie_id_and_rule(store):
    occ, _ = h.create_hearing_series(_proto(), "mensuelle", count=3)
    assert len({o["serie_id"] for o in occ}) == 1
    assert occ[0]["serie_id"]
    assert occ[0]["serie_rule"] == {
        "freq": "mensuelle", "start": "2026-09-15", "count": 3
    }


def test_the_dav_href_follows_each_occurrence_id(store):
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=3)
    for o in occ:
        assert o["dav_href"] == f"/dav/dossier-d1/{o['id']}.ics"


def test_the_ctag_bump_rides_inside_the_batch(store, commits):
    """Commit puis bump laisse N audiences vivantes que DavX5 ne resynchronise
    JAMAIS (sync-collection court-circuite sur un jeton inchangé)."""
    h.create_hearing_series(_proto(), "hebdomadaire", count=4)
    assert len(commits) == 1                      # UN seul commit
    ops = commits[0]
    assert sum(1 for k, c, _ in ops if c == "hearings") == 4
    assert [c for _k, c, _i in ops].count("dav_sync") == 1
    assert ("set", "dav_sync", "dossier:d1") in ops


def test_a_dossier_less_series_bumps_general(store, commits):
    h.create_hearing_series(
        _proto(dossier_id=""), "hebdomadaire", count=2
    )
    assert ("set", "dav_sync", "general") in commits[0]


def test_a_refused_prototype_writes_nothing(store, commits):
    occ, errors = h.create_hearing_series(
        _proto(title=""), "hebdomadaire", count=4
    )
    assert occ == [] and errors
    assert store == {} and commits == []


def test_a_refused_rule_writes_nothing(store, commits):
    occ, errors = h.create_hearing_series(_proto(), "hebdomadaire")
    assert occ == [] and errors
    assert store == {} and commits == []


def test_an_invalid_frequency_writes_nothing(store, commits):
    occ, errors = h.create_hearing_series(_proto(), "quotidienne", count=3)
    assert occ == [] and errors and store == {}


# ── Le temps ────────────────────────────────────────────────────────────
def test_a_timed_series_holds_its_montreal_wall_clock_across_dst(store):
    """9 h reste 9 h de part et d'autre de la bascule de novembre. Ajouter des
    timedelta à la valeur UTC stockée décalerait tout d'une heure."""
    occ, _ = h.create_hearing_series(
        _proto(
            start_datetime=datetime(2026, 10, 14, 13, 0, tzinfo=UTC),
            end_datetime=datetime(2026, 10, 14, 14, 0, tzinfo=UTC),
        ),
        "hebdomadaire",
        count=6,
    )
    assert {to_mtl(o["start_datetime"]).time() for o in occ} == {time(9, 0)}
    # …et la fenêtre a bien traversé la bascule (sinon la garde ci-dessus ne
    # prouverait rien) : EDT = UTC-4, EST = UTC-5.
    assert {o["start_datetime"].hour for o in occ} == {13, 14}


def test_each_occurrence_keeps_the_prototype_duration(store):
    occ, _ = h.create_hearing_series(
        _proto(end_datetime=datetime(2026, 9, 15, 15, 30, tzinfo=UTC)),
        "hebdomadaire",
        count=4,
    )
    assert all(
        o["end_datetime"] - o["start_datetime"] == timedelta(hours=2, minutes=30)
        for o in occ
    )


def test_an_all_day_series_stays_at_utc_midnight(store):
    occ, _ = h.create_hearing_series(_allday(), "hebdomadaire", count=3)
    assert all(o["all_day"] for o in occ)
    assert [o["start_datetime"] for o in occ] == [
        datetime(2026, 9, 15, tzinfo=UTC),
        datetime(2026, 9, 22, tzinfo=UTC),
        datetime(2026, 9, 29, tzinfo=UTC),
    ]


def test_a_late_evening_series_anchors_on_the_montreal_day(store):
    """21 h le 15 à Montréal est stocké le 16 en UTC. Ancrer sur la date UTC
    décalerait toute la série d'un jour."""
    occ, _ = h.create_hearing_series(
        _proto(
            start_datetime=datetime(2026, 9, 16, 1, 0, tzinfo=UTC),  # 21 h le 15
            end_datetime=datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
        ),
        "hebdomadaire",
        count=3,
    )
    assert [to_mtl(o["start_datetime"]).date() for o in occ] == [
        date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29)
    ]
    assert occ[0]["serie_rule"]["start"] == "2026-09-15"


def test_monthly_series_anchors_and_does_not_drift(store):
    occ, _ = h.create_hearing_series(
        _proto(
            start_datetime=datetime(2026, 1, 31, 14, 0, tzinfo=UTC),
            end_datetime=datetime(2026, 1, 31, 15, 0, tzinfo=UTC),
        ),
        "mensuelle",
        count=4,
    )
    assert [to_mtl(o["start_datetime"]).date() for o in occ] == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)
    ]


# ── list_series : le piège de la chaîne vide ────────────────────────────
def test_list_series_refuses_an_empty_id(store):
    """"" est une VALEUR STOCKÉE : une égalité dessus ramènerait toute
    audience autonome du cabinet."""
    h.create_hearing_series(_proto(), "hebdomadaire", count=3)
    store["hearings"]["solo"] = {
        "id": "solo", "serie_id": "", "title": "Audience isolée",
        "start_datetime": datetime(2026, 9, 1, tzinfo=UTC),
    }
    assert h.list_series("") == []
    assert h.list_series(None) == []


def test_list_series_returns_only_its_own_chain(store):
    a, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=3)
    b, _ = h.create_hearing_series(_proto(title="Autre"), "mensuelle", count=2)
    rows = h.list_series(a[0]["serie_id"])
    assert {r["id"] for r in rows} == {o["id"] for o in a}
    assert len(h.list_series(b[0]["serie_id"])) == 2


def test_list_series_is_chronological(store):
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=5)
    rows = h.list_series(occ[0]["serie_id"])
    assert [r["start_datetime"] for r in rows] == sorted(
        r["start_datetime"] for r in rows
    )


def test_list_series_propagates_a_read_failure(store, monkeypatch):
    """Contrairement à list_hearings qui rend []. Un dialogue destructeur ne
    doit jamais sous-estimer ce qu'il va détruire."""
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=3)

    class _Boom:
        def collection(self, name):
            raise RuntimeError("firestore down")

    monkeypatch.setattr(h, "db", _Boom())
    with pytest.raises(RuntimeError):
        h.list_series(occ[0]["serie_id"])


# ── delete_series ───────────────────────────────────────────────────────
def test_delete_series_refuses_an_empty_id(store, commits):
    h.create_hearing_series(_proto(), "hebdomadaire", count=3)
    store["hearings"]["solo"] = {"id": "solo", "serie_id": ""}
    before = len(store["hearings"])
    rows, errors = h.delete_series("")
    assert rows == [] and errors
    assert len(store["hearings"]) == before


def test_delete_series_removes_the_whole_chain(store):
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=4)
    rows, errors = h.delete_series(occ[0]["serie_id"])
    assert errors == [] and len(rows) == 4
    assert store["hearings"] == {}


def test_delete_series_leaves_other_chains_alone(store):
    a, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=3)
    b, _ = h.create_hearing_series(_proto(title="Autre"), "mensuelle", count=2)
    h.delete_series(a[0]["serie_id"])
    assert set(store["hearings"]) == {o["id"] for o in b}


def test_delete_series_from_date_protects_past_occurrences(store):
    """« Cette occurrence et les suivantes » : une occurrence passée est le
    constat de ce qui a eu lieu."""
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=5)
    pivot = date(2026, 9, 29)                       # la 3e occurrence
    rows, errors = h.delete_series(occ[0]["serie_id"], from_date=pivot)
    assert errors == []
    assert [to_mtl(r["start_datetime"]).date() for r in rows] == [
        date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13)
    ]
    assert set(store["hearings"]) == {occ[0]["id"], occ[1]["id"]}


def test_delete_series_from_date_after_the_end_deletes_nothing(store):
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=3)
    rows, errors = h.delete_series(
        occ[0]["serie_id"], from_date=date(2030, 1, 1)
    )
    assert rows == [] and errors == []
    assert len(store["hearings"]) == 3


def test_delete_series_stages_deletes_tombstones_and_bump_in_one_batch(
    store, commits
):
    """Les pierres tombales sont le SEUL signal de retrait de ce modèle de
    synchro : un delete qui commit sans elles laisse les dates sur le
    téléphone pour toujours, et les documents n'existent plus pour rejouer."""
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=4)
    commits.clear()
    h.delete_series(occ[0]["serie_id"])

    assert len(commits) == 1                        # UN seul commit
    ops = commits[0]
    deletes = [o for o in ops if o[0] == "delete" and o[1] == "hearings"]
    tombs = [o for o in ops if o[1] == "dav_sync/dossier:d1/tombstones"]
    bumps = [o for o in ops if o[1] == "dav_sync"]
    assert len(deletes) == 4
    assert {o[2] for o in tombs} == {o["id"] for o in occ}
    assert len(bumps) == 1


def test_delete_series_on_an_unknown_id_is_a_no_op(store):
    h.create_hearing_series(_proto(), "hebdomadaire", count=2)
    rows, errors = h.delete_series("aucune-serie")
    assert rows == [] and errors == []
    assert len(store["hearings"]) == 2


# ── unlink ──────────────────────────────────────────────────────────────
def test_unlink_clears_both_fields(store):
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=3)
    updated, errors = h.unlink_hearing(occ[1]["id"])
    assert errors == []
    assert updated["serie_id"] == "" and updated["serie_rule"] is None
    assert len(h.list_series(occ[0]["serie_id"])) == 2


def test_unlink_refuses_a_hearing_that_is_not_in_a_series(store):
    store.setdefault("hearings", {})["solo"] = {
        "id": "solo", "serie_id": "", "title": "Isolée",
        "start_datetime": datetime(2026, 9, 1, tzinfo=UTC),
    }
    updated, errors = h.unlink_hearing("solo")
    assert updated is None and errors


def test_unlink_refuses_an_unknown_hearing(store):
    updated, errors = h.unlink_hearing("aucune")
    assert updated is None and errors


def test_an_unlinked_occurrence_survives_a_chain_delete(store):
    """Le détachement est la sortie : l'occurrence n'appartient plus à la
    chaîne, donc la suppression de la chaîne ne la voit plus."""
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=4)
    h.unlink_hearing(occ[2]["id"])
    h.delete_series(occ[0]["serie_id"])
    assert set(store["hearings"]) == {occ[2]["id"]}


# ── Le plafond ──────────────────────────────────────────────────────────
def test_the_cap_is_enforced_before_any_write(store, commits):
    occ, errors = h.create_hearing_series(
        _proto(), "hebdomadaire",
        count=recurrence.MAX_SERIE_OCCURRENCES + 1,
    )
    assert occ == [] and errors and store == {} and commits == []


def test_a_series_at_the_cap_is_written_in_one_batch(store, commits):
    occ, errors = h.create_hearing_series(
        _proto(), "hebdomadaire", count=recurrence.MAX_SERIE_OCCURRENCES
    )
    assert errors == [] and len(occ) == recurrence.MAX_SERIE_OCCURRENCES
    assert len(commits) == 1


def test_a_chain_delete_at_the_cap_stays_one_atomic_batch(store, commits):
    """2N + 1 opérations doivent tenir sous le plafond de lot de Firestore —
    deux lots ne seraient PAS atomiques, et l'échec du second laisserait des
    audiences supprimées sans pierre tombale."""
    occ, _ = h.create_hearing_series(
        _proto(), "hebdomadaire", count=recurrence.MAX_SERIE_OCCURRENCES
    )
    commits.clear()
    rows, errors = h.delete_series(occ[0]["serie_id"])
    assert errors == [] and len(rows) == recurrence.MAX_SERIE_OCCURRENCES
    assert len(commits) == 1
    assert 2 * recurrence.MAX_SERIE_OCCURRENCES + 1 <= sync._BATCH_CHUNK


# ── Migration de lecture ────────────────────────────────────────────────
def test_a_legacy_hearing_reads_as_standalone():
    doc = h._migrate_hearing({"id": "old", "title": "Ancienne"})
    assert doc["serie_id"] == "" and doc["serie_rule"] is None


# ── Ce que la série met sur le fil DAV ──────────────────────────────────
def test_each_occurrence_serializes_as_an_ordinary_single_vevent(store):
    """Les occurrences sont MATÉRIALISÉES : chacune est un VEVENT ordinaire.
    Émettre une RRULE en plus ferait étendre le téléphone PAR-DESSUS des
    frères qui existent déjà — chaque rendez-vous compterait double."""
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=4)
    for o in occ:
        ical = h.hearing_to_vevent(o)
        assert ical.count("BEGIN:VEVENT") == 1
        assert "RRULE" not in ical
        assert "RECURRENCE-ID" not in ical
        assert "DTSTAMP:" in ical and "CREATED:" in ical
    # …et chaque occurrence a son propre UID, sinon le client n'en verrait
    # qu'une seule.
    uids = [
        l for o in occ for l in h.hearing_to_vevent(o).splitlines()
        if l.startswith("UID:")
    ]
    assert len(set(uids)) == 4


def test_the_series_link_is_never_put_on_the_wire(store):
    """serie_id appartient au SERVEUR. Ne pas l'émettre est ce qui fait que
    le lien survit gratuitement à un aller-retour depuis le téléphone :
    vevent_to_hearing ne le produit jamais, et update_hearing fusionne, donc
    une clé absente survit."""
    occ, _ = h.create_hearing_series(_proto(), "hebdomadaire", count=2)
    ical = h.hearing_to_vevent(occ[0])
    assert "serie" not in ical.lower()
    assert "SERIE" not in ical
    # Un PUT depuis le téléphone ne peut pas effacer le lien.
    parsed = h.vevent_to_hearing(ical)
    assert "serie_id" not in parsed and "serie_rule" not in parsed


def test_an_all_day_series_emits_valid_exclusive_dtend_on_every_occurrence(
    store,
):
    occ, _ = h.create_hearing_series(_allday(), "hebdomadaire", count=3)
    for o in occ:
        lines = h.hearing_to_vevent(o).splitlines()
        start = [l for l in lines if l.startswith("DTSTART")][0]
        end = [l for l in lines if l.startswith("DTEND")][0]
        assert "VALUE=DATE" in start and "VALUE=DATE" in end
        assert start.split(":")[1] != end.split(":")[1]

