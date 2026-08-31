"""chat/planification.py + models/chat_scheduled_task.py (Phase N §12).

Frozen-clock discipline throughout (the 2026-08-11 00:03 UTC lesson): every
due decision uses FIXED Montréal datetimes, never the wall clock.
"""

import copy
import importlib
import importlib.util
import os
import sys
import types
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _install_stub(name, module):
    parts = name.split(".")
    for i in range(1, len(parts)):
        pkg = ".".join(parts[:i])
        if pkg in sys.modules:
            continue
        if _module_available(pkg):
            importlib.import_module(pkg)
            continue
        pkg_module = types.ModuleType(pkg)
        pkg_module.__path__ = []
        sys.modules[pkg] = pkg_module
        if i > 1:
            setattr(sys.modules[".".join(parts[: i - 1])], parts[i - 1], pkg_module)
    sys.modules[name] = module
    if len(parts) > 1:
        setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)


if not _module_available("google.cloud.firestore"):
    _firestore_stub = types.ModuleType("google.cloud.firestore")

    class _StubQuery:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"

    _firestore_stub.Client = mock.MagicMock(name="firestore.Client")
    _firestore_stub.Query = _StubQuery
    _firestore_stub.Transaction = type("Transaction", (), {})
    _firestore_stub.transactional = lambda func: func
    _install_stub("google.cloud.firestore", _firestore_stub)

with mock.patch("google.cloud.firestore.Client"):
    import models.chat_conversation as cc
    import models.chat_scheduled_task as st
    from chat import planification


_MTL = ZoneInfo("America/Montreal")


# ── Fake Firestore (harness + where-equality for list_active) ──────────────


class _FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def collection(self, name):
        return _FakeCollectionRef(self._store, f"{self._coll}/{self.id}/{name}")

    def get(self, transaction=None):
        return _FakeSnapshot(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = copy.deepcopy(data)

    def update(self, fields):
        doc = self._store.setdefault(self._coll, {}).get(self.id)
        if doc is None:
            raise KeyError(f"update on missing {self._coll}/{self.id}")
        doc.update(copy.deepcopy(fields))


class _FakeQuery:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll
        self._orders = []
        self._filters = []
        self._limit = None

    def where(self, filter=None):
        self._filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def order_by(self, field, direction="ASCENDING"):
        self._orders.append((field, direction))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, doc):
        for fp, op, val in self._filters:
            if op == "==":
                if doc.get(fp) != val:
                    return False
            else:
                raise AssertionError(f"unsupported operator in fake: {op}")
        return True

    def _rows(self):
        rows = [d for d in self._store.get(self._coll, {}).values() if self._match(d)]
        for field, direction in reversed(self._orders):
            rows.sort(
                key=lambda d: (d.get(field) is None, d.get(field)),
                reverse=(direction == "DESCENDING"),
            )
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows

    def stream(self, transaction=None):
        return [_FakeSnapshot(d.get("id"), d) for d in self._rows()]


class _FakeCollectionRef(_FakeQuery):
    def document(self, doc_id):
        return _FakeDocRef(self._store, self._coll, doc_id)


class _FakeTransaction:
    def set(self, ref, data):
        ref.set(data)

    def update(self, ref, fields):
        ref.update(fields)


class _FakeDB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _FakeCollectionRef(self._store, name)

    def transaction(self):
        return _FakeTransaction()


class _FakeFirestore:
    transactional = staticmethod(lambda fn: fn)

    class Query:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"


@pytest.fixture()
def world(monkeypatch):
    store: dict = {}
    events: list = []
    enqueued: list = []
    monkeypatch.setattr(st, "db", _FakeDB(store))
    monkeypatch.setattr(st, "firestore", _FakeFirestore)
    monkeypatch.setattr(cc, "db", _FakeDB(store))
    monkeypatch.setattr(cc, "firestore", _FakeFirestore)
    monkeypatch.setattr(planification, "_owner_uid", lambda: "u1")
    monkeypatch.setattr(
        planification,
        "log_chat_event",
        lambda event, outcome="success", **kw: events.append(
            {"event": event, "outcome": outcome, **kw}
        ),
    )

    import chat.taches as taches

    monkeypatch.setattr(
        taches,
        "enfiler_tour",
        lambda cid, tid, token: enqueued.append((cid, tid, token)),
    )
    return types.SimpleNamespace(store=store, events=events, enqueued=enqueued)


_VALID_TASK = {
    "name": "Relevé du matin",
    "prompt": "Prépare le relevé quotidien du cabinet.",
    "model": "claude-sonnet-5",
    "recurrence": {"kind": "quotidien", "day": 0},
    "hour_local": 7,
}


def _mtl(y, m, d, h, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=_MTL)


# ── est_due — the recurrence matrix (frozen dates) ─────────────────────────

def test_quotidien_due_after_hour_and_self_heals_later_the_same_day():
    task = {**_VALID_TASK, "occurrences": {}}
    assert planification.est_due(task, _mtl(2026, 9, 1, 6, 59)) is None
    assert planification.est_due(task, _mtl(2026, 9, 1, 7, 0)) == "2026-09-01"
    # A missed window: still due at 16h the SAME day (catch-up, `>=`).
    assert planification.est_due(task, _mtl(2026, 9, 1, 16, 0)) == "2026-09-01"


def test_marked_occurrence_is_never_due_twice():
    task = {**_VALID_TASK, "occurrences": {"2026-09-01": {}}}
    assert planification.est_due(task, _mtl(2026, 9, 1, 9, 0)) is None
    assert planification.est_due(task, _mtl(2026, 9, 2, 9, 0)) == "2026-09-02"


def test_jours_ouvrables_skips_weekends_and_quebec_holidays():
    task = {
        **_VALID_TASK,
        "recurrence": {"kind": "jours_ouvrables", "day": 0},
        "occurrences": {},
    }
    # 2026-06-24 (Fête nationale, a Wednesday) — a juridical holiday.
    assert planification.est_due(task, _mtl(2026, 6, 24, 9, 0)) is None
    # 2026-06-27 is a Saturday.
    assert planification.est_due(task, _mtl(2026, 6, 27, 9, 0)) is None
    # 2026-06-25 (Thursday) is an ordinary juridical day.
    assert planification.est_due(task, _mtl(2026, 6, 25, 9, 0)) == "2026-06-25"


def test_hebdomadaire_matches_the_configured_weekday():
    task = {
        **_VALID_TASK,
        "recurrence": {"kind": "hebdomadaire", "day": 0},  # lundi
        "occurrences": {},
    }
    assert planification.est_due(task, _mtl(2026, 9, 7, 8, 0)) == "2026-09-07"
    assert planification.est_due(task, _mtl(2026, 9, 8, 8, 0)) is None


def test_dst_boundary_is_a_non_event():
    # 2026-03-08: the spring-forward Sunday in Montréal. Local-date keying
    # means the occurrence id is simply that date; the shortened day
    # changes nothing.
    task = {**_VALID_TASK, "occurrences": {}}
    assert planification.est_due(task, _mtl(2026, 3, 8, 7, 30)) == "2026-03-08"


# ── The sweep — dispatch + idempotency (acceptance #11) ────────────────────

def _seed_task(world, **overrides):
    doc, errors = st.create_task({**_VALID_TASK, **overrides})
    assert errors == []
    return doc


def test_sweep_dispatches_once_and_never_twice(world, monkeypatch):
    _seed_task(world)
    monkeypatch.setattr(planification, "_now_mtl", lambda: _mtl(2026, 9, 1, 8, 0))
    first = planification.executer_balayage()
    assert first["dispatched"] == 1
    conversations = world.store["chat_conversations"]
    assert len(conversations) == 1
    conv = next(iter(conversations.values()))
    assert conv["origin"] == "planifiee"
    assert conv["unread"] is True
    assert conv["title"] == "Relevé du matin — 2026-09-01"
    turns = world.store[f"chat_conversations/{conv['id']}/turns"]
    assert turns["000001"]["by"] == "planificateur"
    assert turns["000002"]["addendum"] == "unattended"
    assert len(world.enqueued) == 1

    # A duplicate cron delivery dispatches NOTHING twice — est_due reads
    # the marked occurrence and reports nothing due.
    second = planification.executer_balayage()
    assert second == {"dues": 0, "dispatched": 0, "skipped": 0, "repaired": 0}
    assert len(world.store["chat_conversations"]) == 1

    # And the CAS itself holds even against a STALE task snapshot (two
    # racing sweeps reading before either marked): the transaction refuses.
    stale = {k: v for k, v in world.store["chat_scheduled_tasks"].items()}
    task_doc = copy.deepcopy(next(iter(stale.values())))
    task_doc["occurrences"] = {}  # the racing sweep's stale view
    assert planification._dispatch(task_doc, "2026-09-01") is False
    assert len(world.store["chat_conversations"]) == 1
    refused = [
        e for e in world.events
        if e["event"] == "chat_scheduled_dispatch" and e["outcome"] == "refused"
    ]
    assert refused and refused[0]["reason"] == "already_dispatched"


def test_inactive_tasks_are_never_dispatched(world, monkeypatch):
    task = _seed_task(world)
    st.set_active(task["id"], False)
    monkeypatch.setattr(planification, "_now_mtl", lambda: _mtl(2026, 9, 1, 8, 0))
    result = planification.executer_balayage()
    assert result == {"dues": 0, "dispatched": 0, "skipped": 0, "repaired": 0}
    assert "chat_conversations" not in world.store


def test_enqueue_failure_leaves_the_occurrence_marked_then_repair(world, monkeypatch):
    import chat.taches as taches

    _seed_task(world)
    monkeypatch.setattr(planification, "_now_mtl", lambda: _mtl(2026, 9, 1, 8, 0))
    monkeypatch.setattr(
        taches,
        "enfiler_tour",
        mock.Mock(side_effect=RuntimeError("file indisponible")),
    )
    result = planification.executer_balayage()
    assert result["dispatched"] == 1  # the dispatch itself committed
    assert any(e["event"] == "chat_enqueue_failed" for e in world.events)
    conv = next(iter(world.store["chat_conversations"].values()))
    turn = world.store[f"chat_conversations/{conv['id']}/turns"]["000002"]
    assert turn["continuation"]["enqueued"] is False

    # Age the turn past the grace window, restore the queue, sweep again:
    # the repair pass re-enqueues the STORED token, loudly.
    old = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    world.store[f"chat_conversations/{conv['id']}/turns"]["000002"].update(
        {"created_at": old, "updated_at": old}
    )
    repaired_queue: list = []
    monkeypatch.setattr(
        taches,
        "enfiler_tour",
        lambda cid, tid, token: repaired_queue.append(token),
    )
    result = planification.executer_balayage()
    assert result["repaired"] == 1
    assert repaired_queue == [turn["continuation"]["token"]]
    repairs = [e for e in world.events if e["event"] == "chat_scheduled_repair"]
    assert repairs and repairs[0]["outcome"] == "failure"  # ERROR by doctrine


# ── §12.4 — deliver_email, at-most-once ────────────────────────────────────

def _finalized_scheduled_conv(world, deliver_email=True):
    task = _seed_task(world, deliver_email=deliver_email)
    conv, _ = cc.create_conversation(
        {
            "title": "Relevé — 2026-09-01",
            "model": "claude-sonnet-5",
            "origin": "planifiee",
            "scheduled_task_id": task["id"],
            "owner_uid": "u1",
        }
    )
    turn, _ = cc.start_turn(conv["id"], "Rapport", by="planificateur")
    cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="final",
        segment={
            "step": 1, "model": "claude-sonnet-5",
            "blocks": [{"type": "text", "text": "## Rapport\n\nRien à signaler."}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1},
            "pricing": {"usd_micros": 1}, "tool_results": None,
        },
    )
    return cc.get_conversation(conv["id"])


def _finalized_scheduled_conv_empty(world):
    """La même chose, mais dont le tour final ne porte AUCUN texte.

    C'est l'état exact produit par l'incident du 2026-08-31 : quatre appels
    d'outils réussis, puis un cinquième appel rendu sans le moindre bloc.
    """
    task = _seed_task(world, deliver_email=True)
    conv, _ = cc.create_conversation(
        {
            "title": "Relevé vide — 2026-09-01",
            "model": "claude-sonnet-5", "origin": "planifiee",
            "scheduled_task_id": task["id"], "owner_uid": "u1",
        }
    )
    turn, _ = cc.start_turn(conv["id"], "Rapport", by="planificateur")
    cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="final",
        segment={
            "step": 1, "model": "claude-sonnet-5",
            "blocks": [],                       # <- rien
            "stop_reason": "end_turn", "usage": {"input_tokens": 63335},
            "pricing": {"usd_micros": 1}, "tool_results": None,
        },
    )
    return cc.get_conversation(conv["id"])


def test_an_empty_report_sends_a_failure_notice_not_the_report(world, monkeypatch):
    """Un rapport vide ne se livre pas — mais le SILENCE non plus.

    Premier réflexe : ne rien envoyer. C'était une erreur, et l'incident du
    2026-08-31 le prouve — il n'a été DÉTECTÉ que parce qu'un courriel vide
    est arrivé. Une absence, un mardi matin, se confond avec « rien à
    signaler » : la tâche pourrait mourir des semaines sans que personne le
    voie. Un courriel vide entraîne à ignorer le canal ; le silence le
    supprime. On envoie donc un AVIS qui nomme la panne.
    """
    sent: list = []
    monkeypatch.setattr(
        planification.courriel, "envoyer",
        lambda dest, objet, corps: sent.append((dest, objet, corps)),
    )
    conv = _finalized_scheduled_conv_empty(world)
    planification.livrer_rapport(conv)
    assert len(sent) == 1
    _dest, objet, corps = sent[0]
    assert "échec" in objet
    assert "sans produire de rapport" in corps
    # Le marqueur est posé par l'avis : au plus UN courriel par exécution,
    # avis compris, et jamais les deux.
    assert cc.get_conversation(conv["id"]).get("courriel_livre") is True
    planification.livrer_rapport(cc.get_conversation(conv["id"]))
    assert len(sent) == 1
    assert any(
        e["event"] == "chat_report_emailed" and e.get("reason") == "aucun_rapport"
        for e in world.events
    )


def test_interstitial_narration_does_not_pass_for_a_report(world, monkeypatch):
    """La forme que produit une troncature : les appels d'outils narrés,
    puis plus rien.

    Le corps concatène TOUS les segments, si bien qu'une seule ligne de
    narration suffisait à rendre le rapport « non vide » et à le livrer,
    alors que le tour n'a jamais conclu. C'est pourquoi la décision porte
    sur le DERNIER segment, pas sur la concaténation.
    """
    sent: list = []
    monkeypatch.setattr(
        planification.courriel, "envoyer",
        lambda dest, objet, corps: sent.append((dest, objet, corps)),
    )
    task = _seed_task(world, deliver_email=True)
    conv, _ = cc.create_conversation({
        "title": "Relevé narré — 2026-09-01", "model": "claude-sonnet-5",
        "origin": "planifiee", "scheduled_task_id": task["id"],
        "owner_uid": "u1",
    })
    turn, _ = cc.start_turn(conv["id"], "Rapport", by="planificateur")
    token = turn["step_token"]
    for i in (1, 2):
        _st, token = cc.commit_step(
            conv["id"], turn["id"], token, next_state="running",
            segment={"step": i, "model": "claude-sonnet-5",
                     "blocks": [{"type": "text",
                                 "text": f"Je consulte l'outil {i}."}],
                     "stop_reason": "tool_use", "usage": {"input_tokens": 1},
                     "pricing": {"usd_micros": 1}, "tool_results": []},
        )
    cc.commit_step(
        conv["id"], turn["id"], token, next_state="final",
        segment={"step": 3, "model": "claude-sonnet-5", "blocks": [],
                 "stop_reason": "end_turn", "usage": {"input_tokens": 1},
                 "pricing": {"usd_micros": 1}, "tool_results": None},
    )
    planification.livrer_rapport(cc.get_conversation(conv["id"]))
    assert len(sent) == 1
    assert "échec" in sent[0][1]
    assert "Je consulte" not in sent[0][2]


def test_an_unreadable_registre_is_not_called_an_empty_report(world, monkeypatch):
    """« Vide » est une affirmation sur des données ; une lecture ratée n'en
    autorise aucune (la doctrine get_dossier_strict / subtree_members)."""
    sent: list = []
    monkeypatch.setattr(
        planification.courriel, "envoyer",
        lambda dest, objet, corps: sent.append((dest, objet, corps)),
    )
    monkeypatch.setattr(
        planification.conv_model, "list_turns",
        lambda cid: (_ for _ in ()).throw(RuntimeError("firestore")),
    )
    conv = _finalized_scheduled_conv_empty(world)
    planification.livrer_rapport(conv)
    assert len(sent) == 1 and "n'a pas pu être lu" in sent[0][2]
    assert any(
        e["event"] == "chat_report_emailed"
        and e.get("reason") == "registre_illisible"
        for e in world.events
    )


def test_livrer_echec_reaches_the_inbox_from_a_dead_turn(world, monkeypatch):
    """livrer_rapport ne s'atteint QUE sur la branche de succès terminal :
    sans ce second point d'entrée, un tour mort ne disait rien à personne."""
    sent: list = []
    monkeypatch.setattr(
        planification.courriel, "envoyer",
        lambda dest, objet, corps: sent.append((dest, objet, corps)),
    )
    conv = _finalized_scheduled_conv_empty(world)
    planification.livrer_echec(conv)
    assert len(sent) == 1 and "échec" in sent[0][1]
    # Même marqueur : un rapport ne peut plus suivre l'avis.
    planification.livrer_rapport(cc.get_conversation(conv["id"]))
    assert len(sent) == 1


def test_report_email_sends_exactly_once(world, monkeypatch):
    sent: list = []
    monkeypatch.setattr(
        planification.courriel,
        "envoyer",
        lambda dest, objet, corps: sent.append((dest, objet, corps)),
    )
    conv = _finalized_scheduled_conv(world)
    planification.livrer_rapport(conv)
    assert len(sent) == 1
    dest, objet, corps = sent[0]
    assert dest == "test@example.com"
    assert "Relevé — 2026-09-01" in objet
    assert "Rien à signaler" in corps and "<h2" in corps  # rendered markdown
    # At-most-once: the marker blocks any replay.
    planification.livrer_rapport(cc.get_conversation(conv["id"]))
    assert len(sent) == 1
    assert any(e["event"] == "chat_report_emailed" for e in world.events)


def test_report_email_off_by_default_and_graph_failure_never_raises(world, monkeypatch):
    conv = _finalized_scheduled_conv(world, deliver_email=False)
    monkeypatch.setattr(
        planification.courriel,
        "envoyer",
        mock.Mock(side_effect=AssertionError("must not send")),
    )
    planification.livrer_rapport(conv)  # deliver_email False → no-op

    conv2 = _finalized_scheduled_conv(world)
    monkeypatch.setattr(
        planification.courriel,
        "envoyer",
        mock.Mock(side_effect=RuntimeError("panne graph")),
    )
    planification.livrer_rapport(conv2)  # swallowed, logged
    failures = [
        e for e in world.events
        if e["event"] == "chat_report_emailed" and e["outcome"] == "failure"
    ]
    assert failures and failures[0]["reason"] == "graph_error"


# ── Model pins ─────────────────────────────────────────────────────────────

def test_recurrence_vocabulary_is_closed(world):
    doc, errors = st.create_task(
        {**_VALID_TASK, "recurrence": {"kind": "0 7 * * *"}}
    )
    assert doc is None
    assert any("Récurrence" in e for e in errors)
    assert st.VALID_RECURRENCES == ("quotidien", "jours_ouvrables", "hebdomadaire")


def test_no_delete_exists_in_the_task_module():
    for attr in dir(st):
        assert not attr.startswith("delete"), attr
    with open(st.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "def delete" not in source
    assert ".delete(" not in source


def test_cron_route_guard(monkeypatch):
    from flask import Flask

    from routes.taches_chat_cron import taches_chat_cron_bp

    app = Flask(__name__)
    app.register_blueprint(taches_chat_cron_bp)
    client = app.test_client()
    assert client.get("/taches/chat/planification").status_code == 403

    import chat.planification as p

    monkeypatch.setattr(p, "executer_balayage", lambda: {"dues": 0})
    response = client.get(
        "/taches/chat/planification", headers={"X-Appengine-Cron": "true"}
    )
    assert response.status_code == 200
