"""Le connecteur tient les dossiers — lot 4b (famille DOSSIERS, plan D6).

Deux outils, chacun au-dessus des règles du MODÈLE que le lot 4a a posées :

* ``set_dossier_status`` passe par ``services/dossier_dav.run_status_
  transition`` — la porte du formulaire web — avec ``reconcile_always`` :
  la purge DavX5 à la fermeture, le rétablissement à la réouverture,
  FERMÉE avant l'écriture (des membres illisibles refusent, rien n'est
  écrit), et rejouable : redemander le statut stocké est la réparation.
  Une purge incomplète n'est jamais conservée pour rejeu (``_no_replay``) ;
* ``update_dossier_party`` passe par les aides à une entrée de
  ``models/dossier`` (``update_dossier_party``, ``remove_dossier_party``,
  ``refresh_party_names``) : rôles et avocat d'UNE partie, détachement
  d'UN lien (refusé pour le dernier client, une partie signifiée, un client
  qui a eu du fidéicommis au dossier), rafraîchissement des noms.

Tout passe par le vrai client Firestore au-dessus du faux serveur partagé
(``tests/_fake_firestore.py``) — modèles, service, ``dav.sync`` et le
magasin d'idempotence compris — et l'on relit ce qui est STOCKÉ.
"""

import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.output_schemas as output_schemas
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import dossier as dossier_model
    from models import hearing as hearing_model
    from models import note as note_model
    from models import partie as partie_model
    from models import task as task_model
    from services import dossier_dav
    from utils import deadlines

from tests._fake_firestore import install  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync, dossier_dav)

UTC = timezone.utc
DT = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
KEY = "cle-statut-0001"


@pytest.fixture
def db(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.")
                   or n in ("dav.sync", "mcp.write_support"))
               and getattr(m, "db", None) is not None]
    return install(monkeypatch, *modules)


def _contact(db, pid, first, last, **over):
    doc = {**partie_model._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": first, "last_name": last,
           "etag": f"e-{pid}", "created_at": DT, "updated_at": DT}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)


def _entry(pid, name, roles=("demandeur",), avocat_id="", avocat_name=""):
    return {"id": pid, "name": name, "roles": list(roles),
            "avocat_id": avocat_id, "avocat_name": avocat_name}


JEAN = _entry("p1", "Jean Tremblay")
MARIE = _entry("p2", "Marie Tremblay")
ROY = _entry("p3", "Paul Roy", roles=("défendeur",))


def _dossier(db, clients=(JEAN,), opposing=(ROY,), **over) -> str:
    data = {"file_number": "2026-001", "title": "Tremblay c. Lavoie",
            "clients": [dict(c) for c in clients],
            "opposing_parties": [dict(o) for o in opposing]}
    data.update(over)
    doc, errors = dossier_model.create_dossier(data)
    assert errors == [], errors
    return doc["id"]


def _members(did) -> set:
    task, e1 = task_model.create_task({"title": "Préparer", "dossier_id": did})
    note, e2 = note_model.create_note({
        "title": "Recherche", "content": "Premier jet.",
        "category": "recherche", "dossier_id": did})
    hearing, e3 = hearing_model.create_hearing({
        "title": "Audience", "start_datetime": DT, "dossier_id": did})
    assert e1 == e2 == e3 == []
    return {task["id"], note["id"], hearing["id"]}


def _stored(db, did):
    return db.peek(f"dossiers/{did}")


def _tombstones(db, did) -> set:
    return set(db.peek_collection(f"dav_sync/dossier:{did}/tombstones"))


def _ctag(db, did):
    return (db.peek(f"dav_sync/dossier:{did}") or {}).get("ctag")


def _fail_queries_on(monkeypatch, db, collection: str):
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def _fail_tombstone_commits(db, did):
    """Every commit touching this dossier's tombstones fails server-side —
    the status write (a `dossiers/…` commit) is untouched."""
    prefix = f"dav_sync/dossier:{did}/tombstones/"

    def hook(info):
        if any(path.startswith(prefix) for _kind, path in info.ops):
            raise gexc.ServiceUnavailable("injected tombstone failure")

    return db.add_commit_hook(hook)


def _idempotency_entries(db) -> dict:
    return db.peek_collection(write_support.COLLECTION)


# ══════════════════════════════════════════════════════════════════════
# 1. set_dossier_status — the drain and the restore, as the web does them
# ══════════════════════════════════════════════════════════════════════


def test_closing_drains_the_phone_and_says_what_it_did(db):
    did = _dossier(db)
    members = _members(did)
    before = _ctag(db, did)

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "fermé"})

    stored = _stored(db, did)
    assert stored["status"] == "fermé"
    assert stored["updated_via"] == "mcp"
    today = deadlines.today_mtl()
    assert stored["closed_date"].date() == today           # date-only stamp
    assert _tombstones(db, did) == members
    assert _ctag(db, did) != before
    assert payload["outcome"] == "applied"
    assert payload["status_before"] == "actif"
    assert payload["status_after"] == "fermé"
    assert payload["closed_date"] == today.isoformat()
    assert payload["closed_date_before"] is None
    assert payload["dav"] == {"direction": "drain", "resources": 3,
                              "ctag_bumped": True, "complete": True}
    assert payload["entity"]["etag"] == stored["etag"]
    assert any("quittent le téléphone" in w for w in payload["warnings"])


def test_reopening_restores_the_phone_and_names_the_erased_closing_date(db):
    did = _dossier(db, opened_date=datetime(2025, 3, 1, tzinfo=UTC))
    members = _members(did)
    handlers.set_dossier_status({"dossier_id": did, "status": "fermé",
                                 "closed_date": "2026-01-15"})
    assert _tombstones(db, did) == members

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "actif"})

    stored = _stored(db, did)
    assert stored["status"] == "actif" and stored["closed_date"] is None
    assert _tombstones(db, did) == set()
    assert payload["dav"]["direction"] == "restore"
    assert payload["dav"]["complete"] is True
    assert payload["closed_date_before"] == "2026-01-15"
    assert payload["closed_date"] is None
    assert any("2026-01-15" in w and "effacée" in w for w in payload["warnings"])
    assert any("actualiser sa liste des collections" in w
               for w in payload["warnings"])


def test_the_same_status_writes_nothing_and_resyncs_the_phone(db):
    """Asking again for the stored status IS the repair: no model write
    (the dossier's etag does not move), the visibility re-applied."""
    did = _dossier(db, status="fermé")
    members = _members(did)
    etag = _stored(db, did)["etag"]
    before = _ctag(db, did)

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "fermé"})

    assert _stored(db, did)["etag"] == etag
    assert _tombstones(db, did) == members          # re-tombstoned
    assert _ctag(db, did) != before
    assert payload["outcome"] == "unchanged"
    assert payload["dav"]["direction"] == "drain"
    assert any("rien n'a été écrit" in w for w in payload["warnings"])


def test_a_supplied_closing_date_is_kept_and_a_new_one_corrects_it(db):
    did = _dossier(db, opened_date=datetime(2025, 3, 1, tzinfo=UTC))
    handlers.set_dossier_status({"dossier_id": did, "status": "fermé",
                                 "closed_date": "2026-02-10"})
    assert _stored(db, did)["closed_date"] == datetime(2026, 2, 10, tzinfo=UTC)

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "archivé",
                                           "closed_date": "2026-02-12"})
    assert _stored(db, did)["status"] == "archivé"
    assert _stored(db, did)["closed_date"] == datetime(2026, 2, 12, tzinfo=UTC)
    assert payload["closed_date_before"] == "2026-02-10"


@pytest.mark.parametrize("args, fragment", [
    ({"status": "actif", "closed_date": "2026-01-01"}, "n'accompagne que"),
    ({"status": "fermé", "closed_date": "2999-01-01"}, "futur"),
    ({"status": "fermé", "closed_date": "2024-12-31"}, "précède"),
    ({"status": "fermé", "closed_date": "2026-13-01"}, "YYYY-MM-DD"),
])
def test_closing_date_guards_refuse_before_anything_is_written(db, args, fragment):
    did = _dossier(db, opened_date=datetime(2025, 3, 1, tzinfo=UTC))
    before = _stored(db, did)
    with pytest.raises(tools.ToolArgumentError, match=fragment):
        handlers.set_dossier_status({"dossier_id": did, **args})
    assert _stored(db, did) == before
    assert _tombstones(db, did) == set()


def test_an_unknown_dossier_is_refused_by_name(db):
    with pytest.raises(tools.ToolArgumentError, match="list_dossiers"):
        handlers.set_dossier_status({"dossier_id": "nope", "status": "fermé"})


def test_an_unreadable_dossier_is_never_read_as_unknown(db, monkeypatch):
    did = _dossier(db)

    def boom(_id):
        raise gexc.ServiceUnavailable("down")

    monkeypatch.setattr(dossier_model, "get_dossier_strict", boom)
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.set_dossier_status({"dossier_id": did, "status": "fermé"})
    assert str(err.value) == _UNREADABLE
    assert err.value.reason == "read_unavailable"
    assert db.peek(f"dossiers/{did}")["status"] == "actif"


# The ONE refusal of a dossier read that failed (fixes of lot 4) — every
# write that resolves a dossier, never « Dossier introuvable ».
_UNREADABLE = "Le dossier n'a pas pu être lu — réessayez."


def _fail_reads_of(monkeypatch, db, target: str) -> None:
    """Every keyed read of *target* fails at the transport — the blip."""
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        if any(str(n).endswith(target) for n in request["documents"]):
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)


# Every write tool that resolves ONE dossier it names — the creators, the
# dossier's own correction and recorders, the two DOSSIERS tools.
_DOSSIER_RESOLVING_CALLS = {
    "create_note": lambda did: handlers.create_note(
        {"dossier_id": did, "title": "Recherche", "content": "Corps"}),
    "create_task": lambda did: handlers.create_task(
        {"dossier_id": did, "title": "Préparer la requête"}),
    "create_hearing": lambda did: handlers.create_hearing(
        {"dossier_id": did, "title": "Audience", "date": "2026-10-15",
         "start_time": "09:30"}),
    "create_time_entry": lambda did: handlers.create_time_entry(
        {"dossier_id": did, "date": "2026-09-01", "description": "Appel",
         "hours": 1.0}),
    "create_expense": lambda did: handlers.create_expense(
        {"dossier_id": did, "date": "2026-09-01", "description": "Timbre",
         "amount_cents": 1000}),
    "create_protocol": lambda did: handlers.create_protocol(
        {"dossier_id": did, "protocol_type": "conventionnel",
         "start_date": "2026-09-01"}),
    "edit_analyse": lambda did: handlers.edit_analyse({"dossier_id": did}),
    "update_dossier": lambda did: handlers.update_dossier(
        {"dossier_id": did, "title": "Tremblay c. Lavoie (corrigé)"}),
    "complete_dossier": lambda did: handlers.complete_dossier(
        {"dossier_id": did, "domaine": "REC"}),
    "record_signification": lambda did: handlers.record_signification(
        {"dossier_id": did, "partie_id": "p1", "date": "2026-07-15"}),
    "record_prescription_event": lambda did: handlers.record_prescription_event(
        {"dossier_id": did, "type": "renonciation", "date": "2026-07-15"}),
    "set_dossier_status": lambda did: handlers.set_dossier_status(
        {"dossier_id": did, "status": "fermé"}),
    "update_dossier_party": lambda did: handlers.update_dossier_party(
        {"action": "update", "dossier_id": did, "partie_id": "p1",
         "roles": ["intimé"]}),
    "update_dossier_party.refresh_names": lambda did: (
        handlers.update_dossier_party(
            {"action": "refresh_names", "dossier_id": did})),
}


@pytest.mark.parametrize("call", sorted(_DOSSIER_RESOLVING_CALLS))
def test_an_unreadable_dossier_refuses_every_write_that_names_it(
        db, monkeypatch, caplog, call):
    """Fixes of lot 4: _resolve_write_dossier and update_dossier read
    through the fail-open get_dossier, so an outage answered « Dossier
    introuvable » — sending the caller hunting for a dossier that exists,
    or reaching for create_dossier. On the REAL store with the dossier's
    reads failing: the one message, the reason, nothing written."""
    did = _dossier(db)
    _fail_reads_of(monkeypatch, db, f"dossiers/{did}")
    db.reset_logs()
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        with pytest.raises(tools.ToolArgumentError) as err:
            _DOSSIER_RESOLVING_CALLS[call](did)
    assert str(err.value) == _UNREADABLE
    assert err.value.reason == "read_unavailable"
    assert "introuvable" not in str(err.value)
    assert db.commits == []
    assert [r for r in caplog.records if r.name == "pallas.unexpected"
            and "mcp dossier write: dossier unreadable" in r.getMessage()]


def test_the_write_resolution_no_longer_reads_through_the_fail_open_reader():
    """Pinned at the source: the two dossier reads that decide a write go
    through _read_dossier_strict (the fail-open get_dossier stays for the
    displays and the post-commit re-reads)."""
    import inspect

    for fn in (handlers._resolve_write_dossier, handlers._update_dossier_impl):
        source = inspect.getsource(fn)
        assert "_read_dossier_strict(" in source, fn.__name__
        assert "dossier_model.get_dossier(" not in source.split(
            "_raise_if_stale(")[0], fn.__name__


def test_a_dossier_that_does_not_exist_is_still_introuvable(db):
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.create_note({"dossier_id": "nope", "title": "T",
                              "content": "C"})
    assert "Dossier introuvable" in str(err.value)
    assert err.value.reason != "read_unavailable"


@pytest.mark.parametrize("bad_id", ["__x__", ".."])
def test_an_id_the_store_refuses_is_introuvable_never_retried(db, bad_id):
    """A reserved name can hold no dossier (dossier ids are server-minted
    UUIDv4): « réessayez » would send the caller retrying a call that can
    never succeed."""
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.create_task({"dossier_id": bad_id, "title": "T"})
    assert "Dossier introuvable" in str(err.value)
    assert err.value.reason != "read_unavailable"
    assert db.commits == []


def test_unreadable_members_refuse_the_close_and_write_nothing(db, monkeypatch):
    """Fail-closed BEFORE the commit: a close whose tasks cannot be read
    would strand them on the phone — refused, the status untouched."""
    did = _dossier(db)
    _members(did)
    before = _stored(db, did)
    _fail_queries_on(monkeypatch, db, "tasks")
    db.reset_logs()

    with pytest.raises(tools.ToolArgumentError, match="Rien n'a été modifié"):
        handlers.set_dossier_status({"dossier_id": did, "status": "fermé"})

    assert _stored(db, did) == before
    assert db.commits == []


def test_an_incomplete_drain_is_reported_and_never_stored_for_replay(db):
    """The status IS written; the phone is not. The result says so, and it
    is kept OUT of mcp_idempotency — the same-key retry re-runs the drain
    instead of replaying the stale answer."""
    did = _dossier(db)
    members = _members(did)
    remove_hook = _fail_tombstone_commits(db, did)

    first = handlers.set_dossier_status({"dossier_id": did, "status": "fermé",
                                         "idempotency_key": KEY})

    assert _stored(db, did)["status"] == "fermé"
    assert first["dav"]["complete"] is False
    assert first["idempotent_replay"] is False
    assert write_support.NO_REPLAY_KEY not in first
    assert any("MÊME appel (même statut)" in w for w in first["warnings"])
    assert _idempotency_entries(db) == {}
    assert _tombstones(db, did) == set()

    remove_hook()
    second = handlers.set_dossier_status({"dossier_id": did,
                                          "status": "fermé",
                                          "idempotency_key": KEY})
    assert second["idempotent_replay"] is False    # executed, not replayed
    assert second["outcome"] == "unchanged"
    assert second["dav"]["complete"] is True
    assert _tombstones(db, did) == members
    # The repair's own result is final: stored, then replayed.
    third = handlers.set_dossier_status({"dossier_id": did,
                                         "status": "fermé",
                                         "idempotency_key": KEY})
    assert third["idempotent_replay"] is True


def test_a_write_between_the_read_and_the_commit_is_refused_stale(db, monkeypatch):
    did = _dossier(db)
    real = dossier_model.get_dossier_strict

    def racing(dossier_id):
        doc = real(dossier_id)
        db.external_write(f"dossiers/{did}", {**db.peek(f"dossiers/{did}"),
                                              "etag": "e-rival"})
        return doc

    monkeypatch.setattr(dossier_model, "get_dossier_strict", racing)
    with pytest.raises(tools.ToolArgumentError,
                       match="Ce dossier a été modifié") as err:
        handlers.set_dossier_status({"dossier_id": did, "status": "fermé"})
    assert err.value.reason == "stale_etag"
    assert "get_dossier" in str(err.value)
    assert db.peek(f"dossiers/{did}")["status"] == "actif"


def test_closing_names_the_prescription_trust_and_protocol_effects(db, monkeypatch):
    did = _dossier(db, droit_action_date=datetime(2025, 1, 15, tzinfo=UTC),
                   prescription_type="3_ans")
    db.external_write(f"dossiers/{did}", {**_stored(db, did),
                                          "trust_balance": 12500})
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda _d: {"id": "p", "status": "actif"})

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "archivé"})

    text = " ".join(payload["warnings"])
    assert "alertes de prescription" in text
    assert "fidéicommis" in text
    assert "protocole reste « actif »" in text


def test_a_moved_prescription_date_is_said(db):
    """Every save re-derives the « date pour agir »: a stored value the
    recourse fields no longer yield moves on a status change — said."""
    did = _dossier(db, droit_action_date=datetime(2025, 1, 15, tzinfo=UTC),
                   prescription_type="3_ans")
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did),
        "prescription_date": datetime(2030, 1, 1, tzinfo=UTC)})

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "en_attente"})

    assert any("recalculée" in w and "2030-01-01" in w
               for w in payload["warnings"])
    assert any("rapport de couverture" in w for w in payload["warnings"])


def test_the_status_change_is_logged_as_the_connector_s(db, caplog):
    did = _dossier(db)
    with caplog.at_level(logging.INFO, logger="pallas.dossier"):
        handlers.set_dossier_status({"dossier_id": did, "status": "fermé"})
    events = [r.json_fields for r in caplog.records
              if getattr(r, "json_fields", {}).get("event")
              == "dossier_status_changed"]
    assert len(events) == 1
    assert events[0]["via"] == "mcp"
    assert events[0]["status_to"] == "fermé"


def test_update_dossier_still_refuses_a_status_and_names_the_tool(db):
    did = _dossier(db)
    with pytest.raises(tools.ToolArgumentError, match="set_dossier_status"):
        handlers.update_dossier({"dossier_id": did, "status": "fermé"})


# ══════════════════════════════════════════════════════════════════════
# 2. update_dossier_party — one link at a time
# ══════════════════════════════════════════════════════════════════════


def test_update_replaces_one_party_s_roles_and_leaves_every_other_entry(db):
    did = _dossier(db, clients=(JEAN, MARIE))
    before = _stored(db, did)

    payload = handlers.update_dossier_party({
        "action": "update", "dossier_id": did, "partie_id": "p2",
        "roles": ["intervenant"]})

    stored = _stored(db, did)
    assert stored["clients"][0] == before["clients"][0]
    assert stored["opposing_parties"] == before["opposing_parties"]
    assert stored["clients"][1]["roles"] == ["intervenant"]
    assert stored["clients"][1]["name"] == "Marie Tremblay"
    assert payload["outcome"] == "applied"
    assert payload["party"]["roles_before"] == ["demandeur"]
    assert payload["party"]["roles_after"] == ["intervenant"]
    assert payload["entity"]["etag"] == stored["etag"]


def test_update_sets_a_lawyer_by_contact_id_and_snapshots_his_name(db):
    _contact(db, "av1", "Anne", "Roy", prefix="Me",
             contact_role="avocat_adverse")
    did = _dossier(db)
    payload = handlers.update_dossier_party({
        "action": "update", "dossier_id": did, "partie_id": "p3",
        "avocat_partie_id": "av1"})
    entry = _stored(db, did)["opposing_parties"][0]
    assert entry["avocat_id"] == "av1" and entry["avocat_name"] == "Me Anne Roy"
    assert payload["party"]["avocat_name_after"] == "Me Anne Roy"

    handlers.update_dossier_party({
        "action": "update", "dossier_id": did, "partie_id": "p3",
        "avocat_partie_id": ""})
    entry = _stored(db, did)["opposing_parties"][0]
    assert entry["avocat_id"] == "" and entry["avocat_name"] == ""


def test_an_unknown_lawyer_is_refused_naming_where_to_find_one(db):
    did = _dossier(db)
    with pytest.raises(tools.ToolArgumentError, match="list_parties"):
        handlers.update_dossier_party({
            "action": "update", "dossier_id": did, "partie_id": "p3",
            "avocat_partie_id": "ghost"})


def test_the_lawyer_goes_by_one_name_across_the_connector():
    """update_dossier_party names a party's lawyer as the party entries of
    create_dossier and update_dossier do — `avocat_partie_id` (fixes of lot
    4: it took `avocat_id`, the STORED field's name, so one lawyer had two
    argument names across the connector). The OUTPUT keeps the stored name
    (avocat_id_before / avocat_id_after), and the old argument is refused by
    the schema, which lists the supported one."""
    tool_props = tools.TOOLS["update_dossier_party"]["input_schema"]["properties"]
    assert "avocat_partie_id" in tool_props and "avocat_id" not in tool_props
    for tool, key in (("create_dossier", "clients"),
                      ("create_dossier", "opposing_parties"),
                      ("update_dossier", "add_clients"),
                      ("update_dossier", "add_opposing_parties")):
        entry = tools.TOOLS[tool]["input_schema"]["properties"][key]["items"]
        assert "avocat_partie_id" in entry["properties"], (tool, key)
    description = tools.TOOLS["update_dossier_party"]["description"]
    assert "`avocat_partie_id`" in description
    assert "`avocat_id`" not in description
    errors = tools.validate_args(
        tools.TOOLS["update_dossier_party"]["input_schema"],
        {"action": "update", "dossier_id": "d", "partie_id": "p",
         "avocat_id": "av1"})
    assert errors and "`avocat_id` is not a supported argument" in errors[0]
    assert "`avocat_partie_id`" in errors[0]
    out = output_schemas.OUTPUT_SCHEMAS["update_dossier_party"]
    party = out["properties"]["party"]["properties"]
    assert {"avocat_id_before", "avocat_id_after", "avocat_name_after"} <= set(party)


def test_the_roles_already_stored_write_nothing(db):
    did = _dossier(db)
    db.reset_logs()
    payload = handlers.update_dossier_party({
        "action": "update", "dossier_id": did, "partie_id": "p1",
        "roles": ["demandeur"]})
    assert db.commits == []
    assert payload["outcome"] == "unchanged"
    assert any("déjà ainsi" in w for w in payload["warnings"])


def test_a_duplicate_role_is_refused_by_the_model_s_words(db):
    did = _dossier(db)
    with pytest.raises(tools.ToolArgumentError, match="deux fois"):
        handlers.update_dossier_party({
            "action": "update", "dossier_id": did, "partie_id": "p1",
            "roles": ["demandeur", "demandeur"]})


def test_the_derived_dossier_role_change_is_said(db):
    did = _dossier(db)
    payload = handlers.update_dossier_party({
        "action": "update", "dossier_id": did, "partie_id": "p1",
        "roles": ["intimé"]})
    assert payload["role_before"] == "demandeur"
    assert payload["role_after"] == "intimé"
    assert any("{{dossier.role}}" in w for w in payload["warnings"])


def test_a_party_not_on_the_dossier_is_refused_pointing_to_update_dossier(db):
    did = _dossier(db)
    with pytest.raises(tools.ToolArgumentError, match="add_clients"):
        handlers.update_dossier_party({
            "action": "update", "dossier_id": did, "partie_id": "p9",
            "roles": []})


@pytest.mark.parametrize("args, stray", [
    ({"action": "remove", "roles": ["demandeur"]}, "roles"),
    ({"action": "remove", "avocat_partie_id": ""}, "avocat_partie_id"),
    ({"action": "refresh_names", "side": "clients"}, "side"),
    ({"action": "refresh_names", "expected_etag": "x"}, "expected_etag"),
])
def test_an_argument_the_action_does_not_take_is_refused_by_name(db, args, stray):
    did = _dossier(db)
    before = _stored(db, did)
    with pytest.raises(tools.ToolArgumentError, match=f"`{stray}`"):
        handlers.update_dossier_party({"dossier_id": did, "partie_id": "p1",
                                       **args})
    assert _stored(db, did) == before


def test_update_needs_something_to_change(db):
    did = _dossier(db)
    with pytest.raises(tools.ToolArgumentError, match="Rien à modifier"):
        handlers.update_dossier_party({"action": "update", "dossier_id": did,
                                       "partie_id": "p1"})


def test_a_stale_expected_etag_is_refused_and_writes_nothing(db):
    did = _dossier(db)
    before = _stored(db, did)
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.update_dossier_party({
            "action": "update", "dossier_id": did, "partie_id": "p1",
            "roles": ["intimé"], "expected_etag": "e-old"})
    assert err.value.reason == "stale_etag"
    assert _stored(db, did) == before


def test_remove_detaches_the_link_and_journals_it(db):
    did = _dossier(db)
    payload = handlers.update_dossier_party({
        "action": "remove", "dossier_id": did, "partie_id": "p3"})

    stored = _stored(db, did)
    assert stored["opposing_parties"] == []
    assert stored["opposing_party_ids"] == []
    assert payload["outcome"] == "applied"
    assert payload["party"]["journaled"] is True
    rows = list(db.peek_collection("audit_events").values())
    assert [(r["entity_type"], r["entity_id"], r["dossier_id"]) for r in rows] == [
        ("dossier_party", "p3", did)]
    assert any("le contact lui-même reste" in w for w in payload["warnings"])


def test_remove_refuses_the_last_client(db):
    did = _dossier(db)
    with pytest.raises(tools.ToolArgumentError, match="seul client"):
        handlers.update_dossier_party({"action": "remove", "dossier_id": did,
                                       "partie_id": "p1"})


def test_remove_refuses_a_served_party(db):
    did = _dossier(db)
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did),
        "significations": [{"id": "s1", "partie_id": "p3",
                             "date": DT, "mode": "huissier",
                             "huissier_id": "", "pv_document_id": "",
                             "superseded_by": "", "confirmee": True}]})
    with pytest.raises(tools.ToolArgumentError, match="signification"):
        handlers.update_dossier_party({"action": "remove", "dossier_id": did,
                                       "partie_id": "p3"})


def test_remove_refuses_a_client_who_had_trust_funds_on_the_dossier(db):
    did = _dossier(db, clients=(JEAN, MARIE))
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did), "trust_balance_by_client": {"p2": 0}})
    with pytest.raises(tools.ToolArgumentError, match="fidéicommis"):
        handlers.update_dossier_party({"action": "remove", "dossier_id": did,
                                       "partie_id": "p2"})
    assert [c["id"] for c in _stored(db, did)["clients"]] == ["p1", "p2"]


def test_removing_the_first_client_says_the_default_recipient_moves(db):
    did = _dossier(db, clients=(JEAN, MARIE))
    payload = handlers.update_dossier_party({
        "action": "remove", "dossier_id": did, "partie_id": "p1"})
    assert payload["party"]["was_first_client"] is True
    assert any("PREMIER client" in w for w in payload["warnings"])


def test_refresh_names_by_dossier_reports_each_change_and_is_idempotent(db):
    _contact(db, "p1", "Jean-Marc", "Tremblay")
    did = _dossier(db)

    payload = handlers.update_dossier_party({"action": "refresh_names",
                                            "dossier_id": did})

    (row,) = payload["dossiers"]
    assert payload["outcome"] == "applied" and payload["applied"] == 1
    assert row["changes"] == [{"partie_id": "p1", "side": "clients",
                               "field": "name", "before": "Jean Tremblay",
                               "after": "Jean-Marc Tremblay"}]
    assert row["missing_partie_ids"] == ["p3"]
    assert row["etag"] == _stored(db, did)["etag"]
    assert payload["entity"]["etag"] == _stored(db, did)["etag"]
    assert _stored(db, did)["clients"][0]["name"] == "Jean-Marc Tremblay"

    again = handlers.update_dossier_party({"action": "refresh_names",
                                          "dossier_id": did})
    assert again["outcome"] == "unchanged"


def test_refresh_names_of_a_contact_cited_nowhere_says_so(db):
    _contact(db, "p8", "Luc", "Seul")
    payload = handlers.update_dossier_party({"action": "refresh_names",
                                            "partie_id": "p8"})
    assert payload["dossiers"] == [] and payload["outcome"] == "unchanged"
    assert any("aucun dossier" in w for w in payload["warnings"])


def test_refresh_names_needs_exactly_one_selector(db):
    did = _dossier(db)
    with pytest.raises(tools.ToolArgumentError, match="exactement un"):
        handlers.update_dossier_party({"action": "refresh_names"})
    with pytest.raises(tools.ToolArgumentError, match="exactement un"):
        handlers.update_dossier_party({"action": "refresh_names",
                                       "dossier_id": did, "partie_id": "p1"})


def _fail_idempotency_releases(db):
    """Every DELETE of an idempotency entry fails server-side — the release
    of a claim, on a store blip. The claim then stays PENDING."""
    def hook(info):
        if any(kind == "delete" and path.startswith(
                f"{write_support.COLLECTION}/") for kind, path in info.ops):
            raise gexc.ServiceUnavailable("injected release failure")

    return db.add_commit_hook(hook)


def _retry_warning(payload) -> str:
    (text,) = [w for w in payload["warnings"] if "NOUVELLE clé" in w]
    return text


def test_every_way_the_incomplete_drain_s_retry_text_names_leads_to_the_repair(
        db, monkeypatch):
    """Fixes of lot 4: the texts promised « the same idempotency_key is
    fine » without exception. It is not, when the release of this call's
    claim fails on a store blip: the claim stays pending, and the same-key
    retry is refused « encore en cours », then « interrompu ». The warning
    now names all three outcomes and the way out of each — and following it,
    branch by branch, on the real store, repairs the phone."""
    did = _dossier(db)
    members = _members(did)
    remove_drain_failure = _fail_tombstone_commits(db, did)
    remove_release_failure = _fail_idempotency_releases(db)
    args = {"dossier_id": did, "status": "fermé", "idempotency_key": KEY}

    first = handlers.set_dossier_status(dict(args))
    assert first["dav"]["complete"] is False
    text = _retry_warning(first)
    assert "MÊME clé" in text and "« encore en cours »" in text
    assert "« interrompu »" in text and "get_dossier" in text
    # The release failed: the claim is still there, pending.
    (entry,) = _idempotency_entries(db).values()
    assert entry["status"] == "pending"
    remove_drain_failure()
    remove_release_failure()

    # 1. « encore en cours » → wait, then the SAME key.
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.set_dossier_status(dict(args))
    assert err.value.reason == "idempotency_in_flight"
    assert "encore en cours" in str(err.value)

    # 2. Past the window: « interrompu » → re-read, then a NEW key.
    later = write_support._now() + write_support.IN_FLIGHT_WINDOW + (
        write_support.IN_FLIGHT_WINDOW / 10)
    monkeypatch.setattr(write_support, "_now", lambda: later)
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.set_dossier_status(dict(args))
    assert err.value.reason == "idempotency_interrupted"
    assert "interrompu" in str(err.value)

    reread = handlers.get_dossier({"dossier_id": did})
    status_read = reread["dossier"]["status"]
    assert status_read == "fermé"
    repaired = handlers.set_dossier_status({
        "dossier_id": did, "status": status_read,
        "idempotency_key": "cle-statut-0002"})
    assert repaired["dav"]["complete"] is True
    assert repaired["outcome"] == "unchanged"
    assert _tombstones(db, did) == set(members)


def test_the_moved_status_repair_names_a_new_key_for_its_other_call(db):
    """When the status moved during the call, the repair asks for ANOTHER
    status — other arguments: the same key could then be refused as a
    conflict while this call's claim is held. The warning says NEW key."""
    text = handlers._NO_REPLAY_RETRY
    assert "MÊME clé" in text and "NOUVELLE clé" in text
    source = pathlib.Path(handlers.__file__).read_text(encoding="utf-8")
    moved = source.split("if not dav.complete and (moved or moving):")[1]
    moved = moved.split("elif not dav.complete:")[0]
    assert "NOUVELLE" in moved and "statut RELU" in moved


def test_a_refresh_with_a_refused_dossier_is_never_stored_for_replay(db, monkeypatch):
    """A refused row is repaired by the same call: that result must not be
    replayed to a same-key retry for 24 h."""
    _contact(db, "p1", "Jean-Marc", "Tremblay")
    d1 = _dossier(db)
    _dossier(db, file_number="2026-002")
    real = dossier_model.get_dossier

    def racing(doc_id):
        doc = real(doc_id)
        if doc_id == d1:
            db.external_write(f"dossiers/{d1}", {**db.peek(f"dossiers/{d1}"),
                                                 "etag": "e-rival"})
        return doc

    monkeypatch.setattr(dossier_model, "get_dossier", racing)
    payload = handlers.update_dossier_party({
        "action": "refresh_names", "partie_id": "p1",
        "idempotency_key": "cle-rafraichir-01"})

    assert payload["outcome"] == "partial"
    assert payload["refused"] == 1 and payload["applied"] == 1
    assert write_support.NO_REPLAY_KEY not in payload
    assert _idempotency_entries(db) == {}
    # The retry text is true in every case (fixes of lot 4): the same key
    # normally, « encore en cours » → the same key later, « interrompu » →
    # a re-read and a NEW key (a name already refreshed reads « inchangé »).
    text = _retry_warning(payload)
    assert "MÊME clé" in text and "« encore en cours »" in text
    assert "« interrompu »" in text and "inchangé" in text
    assert "entity" not in payload        # a contact's batch has no one entity


# ══════════════════════════════════════════════════════════════════════
# 4. Review of step 3 — the result says what the store and the phone hold
# ══════════════════════════════════════════════════════════════════════


def _reopen_during_the_drain(db, did, *, times: int):
    """Another writer (the application, another call) flips the dossier's
    status while this call's DAV markers commit — *times* times at most."""
    prefix = f"dav_sync/dossier:{did}/"
    flips: list = []

    def hook(info):
        if len(flips) >= times:
            return
        if any(p.startswith(prefix) for _k, p in info.ops):
            flips.append(1)
            cur = db.peek(f"dossiers/{did}")
            db.external_write(f"dossiers/{did}", {
                **cur, "status": "actif" if cur["status"] == "fermé" else "fermé",
                "etag": f"e-rival-{len(flips)}"})

    return db.add_commit_hook(hook)


def test_a_status_changed_again_during_the_drain_is_reported_as_stored(db):
    """The service follows the STORED status when another writer changed it
    during the drain — the phone is restored. The result used to report the
    status this call wrote (« fermé ») and « its tasks leave the phone »:
    both false. It now says what the store and the phone hold."""
    did = _dossier(db)
    members = _members(did)
    _reopen_during_the_drain(db, did, times=1)

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "fermé"})

    assert _stored(db, did)["status"] == "actif"
    assert _tombstones(db, did).isdisjoint(members)      # phone: restored
    assert payload["dav"]["direction"] == "restore"
    assert payload["dav"]["complete"] is True
    assert payload["status_after"] == "actif"
    text = " ".join(payload["warnings"])
    assert "quittent le téléphone" not in text
    assert "a de nouveau changé pendant l'appel" in text
    assert "« actif »" in text and "get_dossier" in text


def test_a_status_still_moving_never_asks_to_resend_this_call_s_status(db):
    """Still moving after the service's second look: INCOMPLETE — and
    « call again with the SAME status » would overwrite the other writer's
    status. The repair starts with a re-read; nothing is stored for replay."""
    did = _dossier(db)
    _members(did)
    _reopen_during_the_drain(db, did, times=2)

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "fermé",
                                           "idempotency_key": KEY})

    assert payload["dav"]["complete"] is False
    text = " ".join(payload["warnings"])
    assert "avec le MÊME statut" not in text
    assert "statut RELU" in text and "get_dossier" in text
    assert _idempotency_entries(db) == {}


def test_closing_cites_the_effective_date_the_alerts_announce(db):
    """A reconnaissance restarts the delay: the alerts announce the
    EFFECTIVE date (derive_prescription), which the closing warning must
    cite — never the raw prescription_date it replaced."""
    did = _dossier(db, droit_action_date=datetime(2024, 1, 15, tzinfo=UTC),
                   prescription_type="3_ans")
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did),
        "prescription_events": [{
            "id": "ev1", "type": "interruption_reconnaissance",
            "date": datetime(2026, 6, 1, tzinfo=UTC), "end_date": None,
            "reference": "", "document_id": ""}]})
    stored = _stored(db, did)
    raw = handlers.date_str(stored["prescription_date"])
    effective = handlers.date_str(
        dossier_model.derive_prescription(stored)["date_effective"])
    assert raw != effective

    payload = handlers.set_dossier_status({"dossier_id": did,
                                           "status": "fermé"})

    alert = next(w for w in payload["warnings"]
                 if "alertes de prescription" in w)
    assert effective in alert and raw not in alert


def test_an_unreadable_dossier_is_never_a_missing_party_dossier(db, monkeypatch):
    """update_dossier_party read the dossier through the fail-open
    get_dossier: an outage answered « Dossier introuvable », sending the
    caller to look for a dossier that exists. Read strictly now, as
    set_dossier_status reads it."""
    did = _dossier(db)
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        if any(f"dossiers/{did}" in str(n) for n in request["documents"]):
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.update_dossier_party({
            "action": "update", "dossier_id": did, "partie_id": "p1",
            "roles": ["intimé"]})
    assert str(err.value) == _UNREADABLE
    assert err.value.reason == "read_unavailable"


@pytest.mark.parametrize("selector, path", [
    ("dossier_id", "dossiers/{did}"),
    ("partie_id", "parties/p1"),
], ids=["by_dossier", "by_contact"])
def test_a_refresh_over_an_unreadable_record_is_never_introuvable(
        db, monkeypatch, selector, path):
    """refresh_names handed its selector to the model, whose reads fail
    open: an outage answered « Dossier introuvable » / « Contact
    introuvable ». Resolved strictly in the handler now."""
    _contact(db, "p1", "Jean", "Tremblay")
    did = _dossier(db)
    target = path.format(did=did)
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        if any(str(n).endswith(target) for n in request["documents"]):
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    value = did if selector == "dossier_id" else "p1"
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.update_dossier_party({"action": "refresh_names",
                                       selector: value})
    assert "pas pu être lu" in str(err.value)
    assert "introuvable" not in str(err.value)
