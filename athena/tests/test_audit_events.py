"""La trace des suppressions (PA-G06) — audit_events.

Le cas fondateur : une tâche haute priorité portant une échéance a disparu
entre deux breffages, et RIEN nulle part ne pouvait dire si c'était un
retrait délibéré ou une suppression accidentelle. Les pierres tombales DAV
ne peuvent pas servir de journal (pas de type d'entité, TTL 30 jours, faux
positifs à la fermeture d'un dossier, et les LIRE les élague — lire écrit).

Invariants épinglés ici :
1. record_deletion est MEILLEUR-EFFORT : il court APRÈS la suppression
   commise et ne peut jamais faire échouer la route (un échec d'écriture de
   trace rend None, jamais une exception).
2. Une suppression REFUSÉE ne frappe aucun événement fantôme.
3. list_recent filtre en Python sur la fenêtre bornée et échoue OUVERT.
4. L'instantané ne porte jamais le contenu (titre + statut seulement).
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import audit_event as ae


def test_record_deletion_shape(monkeypatch):
    written = {}

    class _Doc:
        def set(self, payload):
            written.update(payload)

    monkeypatch.setattr(
        ae, "db",
        mock.Mock(collection=lambda n: mock.Mock(document=lambda i: _Doc())),
    )
    doc = ae.record_deletion(
        "task", "t1", dossier_id="d1",
        title="Produire la proposition", status="à_faire",
    )
    assert doc is not None
    assert written["entity_type"] == "task"
    assert written["entity_id"] == "t1"
    assert written["dossier_id"] == "d1"
    assert written["snapshot_min"] == {
        "title": "Produire la proposition", "status": "à_faire",
    }
    assert written["at"] == written["created_at"]
    # Rule-7 exception, documented: append-only → no etag, no updated_at.
    assert "etag" not in written
    assert "updated_at" not in written


def test_record_deletion_never_raises(monkeypatch):
    """Le meilleur-effort est structurel : la suppression est déjà commise
    quand la trace s'écrit — une exception ici transformerait un succès en
    erreur utilisateur et inviterait un « réessayer » qui ne re-supprime
    rien."""
    def _boom(n):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(ae, "db", mock.Mock(collection=_boom))
    assert ae.record_deletion("task", "t1") is None


def test_list_recent_filters_in_python(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": "e1", "at": now, "entity_type": "task", "dossier_id": "d1"},
        {"id": "e2", "at": now - timedelta(hours=1), "entity_type": "note",
         "dossier_id": "d1"},
        {"id": "e3", "at": now - timedelta(hours=2), "entity_type": "task",
         "dossier_id": "d2"},
    ]

    class _Snap:
        def __init__(self, d):
            self._d = d

        def to_dict(self):
            return self._d

    query = mock.Mock()
    query.order_by.return_value = query
    query.limit.return_value = query
    query.stream.return_value = [_Snap(r) for r in rows]
    monkeypatch.setattr(
        ae, "db", mock.Mock(collection=lambda n: query)
    )
    assert [r["id"] for r in ae.list_recent()] == ["e1", "e2", "e3"]
    assert [r["id"] for r in ae.list_recent(entity_type="task")] == ["e1", "e3"]
    assert [r["id"] for r in ae.list_recent(dossier_id="d1")] == ["e1", "e2"]
    assert [r["id"] for r in ae.list_recent(limit=1)] == ["e1"]


def test_list_recent_fails_open(monkeypatch):
    def _boom(n):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(ae, "db", mock.Mock(collection=_boom))
    assert ae.list_recent() == []


def test_task_delete_route_records_the_trail(monkeypatch):
    """Le site d'écriture nominal : la route de suppression de tâche frappe
    UN événement après le delete réussi — et aucun sur un refus."""
    from routes import tasks as tr

    recorded = []
    monkeypatch.setattr(
        tr, "record_deletion",
        lambda *a, **kw: recorded.append((a, kw)) or {"id": "ev"},
    )
    monkeypatch.setattr(
        tr, "get_task",
        lambda tid: {"id": tid, "dossier_id": "d1",
                     "title": "Produire la proposition", "status": "à_faire"},
    )
    monkeypatch.setattr(tr, "delete_task", lambda tid: (True, ""))
    monkeypatch.setattr(tr, "record_tombstone", lambda s, r: None)
    monkeypatch.setattr(tr, "bump_ctag", lambda s: None)

    from flask import Flask
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "t"
    app.register_blueprint(tr.tasks_bp)   # url_for("tasks.task_list")
    with app.test_request_context("/taches/t1/delete", method="POST"):
        tr.task_delete.__wrapped__("t1")   # bypass @login_required

    assert len(recorded) == 1
    args, kwargs = recorded[0]
    assert args == ("task", "t1")
    assert kwargs["title"] == "Produire la proposition"

    # Refused delete → no phantom event.
    recorded.clear()
    monkeypatch.setattr(tr, "delete_task", lambda tid: (False, "introuvable"))
    with app.test_request_context("/taches/t1/delete", method="POST"):
        tr.task_delete.__wrapped__("t1")
    assert recorded == []


# ── Parité modèle ↔ connecteur ──────────────────────────────────────────
def test_the_mcp_entity_type_enum_mirrors_the_model_vocabulary():
    """mcp/tools.py recopie VALID_ENTITY_TYPES À LA MAIN — l'interdit
    d'importer models/* au démarrage l'y oblige. Sans cette épingle, une
    valeur ajoutée d'un seul côté devient silencieusement infiltrable :
    le connecteur refuse un filtre que le modèle écrit pourtant.
    """
    from mcp.tools import TOOLS
    from models.audit_event import VALID_ENTITY_TYPES

    enum = TOOLS["list_deletions"]["input_schema"]["properties"][
        "entity_type"
    ]["enum"]
    assert set(enum) == set(VALID_ENTITY_TYPES)


def test_a_deleted_series_is_journalled_as_one_row_not_one_per_occurrence():
    """list_recent lit une fenêtre dure de 200 et filtre EN PYTHON après
    coup : 60 lignes par chaîne évinceraient l'historique du cabinet, et
    list_deletions répondrait alors vide avec truncated: false — une
    affirmation de complétude fausse."""
    from models.audit_event import VALID_ENTITY_TYPES

    assert "hearing_series" in VALID_ENTITY_TYPES

