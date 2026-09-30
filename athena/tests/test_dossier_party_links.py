"""Les liens de parties d'un dossier — lot 4a, étape 2 (plan D6).

Trois règles vivent désormais dans le MODÈLE (``models/dossier.py``), là où
le formulaire web — qui poste les tableaux ``clients`` / ``opposing_parties``
ENTIERS — et le connecteur (lot 4b, une entrée à la fois) les rencontrent
tous deux :

* un contact ne devient jamais client ET partie adverse du même dossier —
  une règle qui ne peut que ne pas GRANDIR (un doublon hérité ne verrouille
  pas le dossier) ;
* une partie qu'une signification nomme ne quitte pas le dossier ;
* un client qui a EU une écriture au fidéicommis du dossier ne quitte pas
  ses clients (lecture stricte, échec fermé).

Le défaut d'origine que la première règle de fidéicommis corrige était
atteignable DEPUIS LE WEB : le modèle ne gardait rien, et le miroir
``client_ids`` (reconstruit APRÈS la validation) n'aurait rien vu si l'on
avait comparé dessus — ces tests ne postent donc JAMAIS le miroir, comme le
formulaire.

Plus les trois outils de modèle (``update_dossier_party``,
``remove_dossier_party``, ``refresh_party_names``) et le journal
``audit_events`` (``dossier_party``). Tout passe par le vrai client
Firestore au-dessus du faux serveur partagé ; on relit ce qui est STOCKÉ.
"""

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
    from models import audit_event as audit_event_model
    from models import concurrency
    from models import dossier as dossier_model
    from models import partie as partie_model
    from models import trust as trust_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, dossier_model, partie_model, trust_model,
                   audit_event_model)


def _contact(db, pid, first, last, **over):
    doc = {**partie_model._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": first, "last_name": last,
           "etag": f"e-{pid}", "created_at": DT, "updated_at": DT}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)


def _entry(pid, name, roles=("demandeur",), avocat_id="", avocat_name=""):
    return {"id": pid, "name": name, "roles": list(roles),
            "avocat_id": avocat_id, "avocat_name": avocat_name}


def _dossier(db, clients, opposing=(), **over):
    data = {"file_number": "2026-001", "title": "Tremblay c. Lavoie",
            "clients": [dict(c) for c in clients],
            "opposing_parties": [dict(o) for o in opposing]}
    data.update(over)
    doc, errors = dossier_model.create_dossier(data)
    assert errors == [], errors
    return doc["id"]


def _stored(db, did):
    return db.peek(f"dossiers/{did}")


def _audit_rows(db):
    return list(db.peek_collection("audit_events").values())


def _fail_queries_on(monkeypatch, db, collection):
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def _count_queries_on(monkeypatch, db, collection) -> list:
    server = db._fake_server
    real = server.run_query
    seen: list = []

    def counting(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            seen.append(1)
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", counting)
    return seen


JEAN = _entry("p1", "Jean Tremblay")
MARIE = _entry("p2", "Marie Tremblay")
ROY = _entry("p3", "Paul Roy", roles=("défendeur",))


# ══════════════════════════════════════════════════════════════════════
# 1. Le fidéicommis : un client qui en a eu ne quitte pas le dossier
# ══════════════════════════════════════════════════════════════════════


def test_a_client_holding_trust_keys_cannot_leave_through_the_arrays(db):
    """LE défaut, atteignable depuis le web : le formulaire poste le seul
    tableau ``clients`` (jamais ``client_ids``), et l'ancien modèle
    l'acceptait — les fonds de Marie devenaient irrécupérables depuis le
    dossier (``client_hors_dossier``)."""
    did = _dossier(db, [JEAN, MARIE])
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did), "trust_balance": 0,
        "trust_balance_by_client": {"p2": 0},
        "trust_cleared_by_client": {"p2": 0},
    })
    before = _stored(db, did)

    doc, errors = dossier_model.update_dossier(did, {"clients": [dict(JEAN)]})

    assert doc is None and len(errors) == 1
    assert "fidéicommis" in errors[0] and "Marie Tremblay" in errors[0]
    assert _stored(db, did) == before


def test_a_client_with_only_a_register_row_cannot_leave_either(db):
    """« EVER », pas « actuellement » : aucune clé sur le dossier, mais une
    ligne du registre nomme le couple (dossier, client)."""
    did = _dossier(db, [JEAN, MARIE])
    db.seed("trust_transactions/t1", {
        "id": "t1", "dossier_id": did, "client_id": "p2", "sequence": 1,
        "account_id": "a1", "direction": "recette", "amount": 100,
    })

    doc, errors = dossier_model.update_dossier(did, {"clients": [dict(JEAN)]})

    assert doc is None and "fidéicommis" in errors[0]
    assert dossier_model._entry_ids(_stored(db, did)["clients"]) == ["p1", "p2"]


def test_an_unreadable_register_refuses_the_removal(db, monkeypatch):
    did = _dossier(db, [JEAN, MARIE])
    _fail_queries_on(monkeypatch, db, "trust_transactions")

    doc, errors = dossier_model.update_dossier(did, {"clients": [dict(JEAN)]})

    assert doc is None
    assert errors == [dossier_model.PARTY_TRUST_CHECK_UNAVAILABLE]
    assert len(_stored(db, did)["clients"]) == 2


def test_a_client_without_trust_history_leaves_and_is_journaled(db):
    did = _dossier(db, [JEAN, MARIE])

    doc, errors = dossier_model.update_dossier(did, {"clients": [dict(JEAN)]})

    assert errors == []
    assert _stored(db, did)["client_ids"] == ["p1"]
    (row,) = _audit_rows(db)
    assert row["entity_type"] == "dossier_party"
    assert row["entity_id"] == "p2" and row["dossier_id"] == did
    assert row["snapshot_min"] == {"title": "Marie Tremblay",
                                   "status": "clients"}


def test_an_opposing_party_removal_never_reads_the_register(db, monkeypatch):
    did = _dossier(db, [JEAN], opposing=[ROY])
    queries = _count_queries_on(monkeypatch, db, "trust_transactions")

    doc, errors = dossier_model.update_dossier(did, {"opposing_parties": []})

    assert errors == [] and queries == []
    assert _stored(db, did)["opposing_party_ids"] == []


def test_an_edit_that_removes_nobody_never_reads_the_register(db, monkeypatch):
    did = _dossier(db, [JEAN, MARIE])
    queries = _count_queries_on(monkeypatch, db, "trust_transactions")

    _doc, errors = dossier_model.update_dossier(did, {"sommaire": "Résumé."})

    assert errors == [] and queries == []
    assert _audit_rows(db) == []


# ══════════════════════════════════════════════════════════════════════
# 2. Un même contact des deux côtés : la règle ne peut que ne pas grandir
# ══════════════════════════════════════════════════════════════════════


def test_a_new_dossier_with_a_contact_on_both_sides_is_refused(db):
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Tremblay",
        "clients": [dict(JEAN)],
        "opposing_parties": [_entry("p1", "Jean Tremblay", ("défendeur",))],
    })
    assert doc is None
    assert "à la fois client et partie adverse" in errors[0]
    assert "Jean Tremblay" in errors[0]
    assert db.peek_collection("dossiers") == {}


def test_an_update_that_grows_the_intersection_is_refused(db):
    did = _dossier(db, [JEAN], opposing=[ROY])
    before = _stored(db, did)

    doc, errors = dossier_model.update_dossier(did, {
        "opposing_parties": [dict(ROY), _entry("p1", "Jean Tremblay")],
    })

    assert doc is None and "à la fois client et partie adverse" in errors[0]
    assert _stored(db, did) == before


def test_a_legacy_duplicate_does_not_lock_the_dossier(db):
    """Grow-only: un dossier hérité qui porte déjà le doublon reste
    modifiable — une règle dure l'aurait verrouillé à jamais."""
    did = _dossier(db, [JEAN], opposing=[ROY])
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did),
        "opposing_parties": [dict(ROY), _entry("p1", "Jean Tremblay")],
        "opposing_party_ids": ["p3", "p1"],
    })

    _doc, errors = dossier_model.update_dossier(did, {"sommaire": "Résumé."})
    assert errors == []

    # …and the repair — removing one side of the duplicate — is a detach
    # of THAT side, journaled as such.
    _doc, errors = dossier_model.update_dossier(
        did, {"opposing_parties": [dict(ROY)]})
    assert errors == []
    assert _stored(db, did)["opposing_party_ids"] == ["p3"]
    (row,) = _audit_rows(db)
    assert row["entity_id"] == "p1"
    assert row["snapshot_min"]["status"] == "opposing_parties"


def test_moving_a_party_to_the_other_side_is_not_a_detach(db):
    did = _dossier(db, [JEAN], opposing=[ROY, _entry("p4", "Luc Roy")])

    _doc, errors = dossier_model.update_dossier(did, {
        "clients": [dict(JEAN), _entry("p4", "Luc Roy")],
        "opposing_parties": [dict(ROY)],
    })

    assert errors == []
    assert _audit_rows(db) == []


# ══════════════════════════════════════════════════════════════════════
# 3. Une partie signifiée reste au dossier
# ══════════════════════════════════════════════════════════════════════


def _served(db):
    did = _dossier(db, [JEAN], opposing=[ROY])
    _doc, errors = dossier_model.update_dossier(did, {"significations": [
        {"partie_id": "p3", "date": "2026-03-01", "mode": "huissier"},
    ]})
    assert errors == []
    return did


def test_a_served_party_cannot_leave_and_the_refusal_names_it(db):
    did = _served(db)
    before = _stored(db, did)

    doc, errors = dossier_model.update_dossier(did, {"opposing_parties": []})

    assert doc is None
    assert errors == [
        "Une partie signifiée ne peut pas être retirée du dossier : Paul Roy "
        "— une signification au dossier la nomme."
    ]
    assert _stored(db, did) == before


def test_removing_the_party_and_its_signification_together_is_a_correction(db):
    """The web form edits the signification repeater too: a party added by
    mistake with a mistaken signification is corrected in ONE save."""
    did = _served(db)

    _doc, errors = dossier_model.update_dossier(
        did, {"opposing_parties": [], "significations": []})

    assert errors == []
    assert _stored(db, did)["opposing_party_ids"] == []


# ══════════════════════════════════════════════════════════════════════
# 4. update_dossier_party
# ══════════════════════════════════════════════════════════════════════


def test_update_rebuilds_from_the_stored_array_touching_one_entry(db):
    did = _dossier(db, [JEAN, MARIE], opposing=[ROY])
    before = _stored(db, did)

    doc, errors, report = dossier_model.update_dossier_party(
        did, "p2", roles=["défendeur reconventionnel", "demandeur"])

    assert errors == [] and report["changed"] is True
    stored = _stored(db, did)
    assert stored["clients"][0] == before["clients"][0]
    assert stored["opposing_parties"] == before["opposing_parties"]
    assert stored["clients"][1] == {
        **before["clients"][1],
        "roles": ["défendeur reconventionnel", "demandeur"],
    }
    assert stored["clients"][1]["name"] == "Marie Tremblay"  # snapshot kept
    assert report["side"] == "clients"
    assert report["roles_before"] == ["demandeur"]
    assert doc["etag"] == stored["etag"] != before["etag"]


def test_update_reports_the_derived_dossier_role(db):
    did = _dossier(db, [JEAN, MARIE])

    _doc, errors, report = dossier_model.update_dossier_party(
        did, "p1", roles=["intimé"])

    assert errors == []
    assert report["role_before"] == "demandeur"
    assert report["role_after"] == "intimé"
    assert _stored(db, did)["role"] == "intimé"


@pytest.mark.parametrize("roles, message", [
    (["capitaine"], "Rôle de partie invalide."),
    (["demandeur", "demandeur"], "Un même rôle figure deux fois."),
    ("demandeur", "Les rôles doivent être une liste."),
])
def test_update_refuses_bad_roles_and_writes_nothing(db, roles, message):
    did = _dossier(db, [JEAN])
    before = _stored(db, did)
    db.reset_logs()

    doc, errors, report = dossier_model.update_dossier_party(
        did, "p1", roles=roles)

    assert doc is None and errors == [message] and report["changed"] is False
    assert _stored(db, did) == before and db.commits == []


def test_update_sets_the_lawyer_from_the_live_contact(db):
    _contact(db, "av1", "Anne", "Roy", contact_role="avocat_adverse",
             prefix="Me")
    did = _dossier(db, [JEAN], opposing=[ROY])

    _doc, errors, report = dossier_model.update_dossier_party(
        did, "p3", avocat_id="av1")

    assert errors == []
    entry = _stored(db, did)["opposing_parties"][0]
    assert entry["avocat_id"] == "av1" and entry["avocat_name"] == "Me Anne Roy"
    assert _stored(db, did)["avocat_ids"] == ["av1"]
    assert report["avocat_name_after"] == "Me Anne Roy"

    # "" clears the link — and its snapshot.
    _doc, errors, report = dossier_model.update_dossier_party(
        did, "p3", avocat_id="")
    assert errors == []
    entry = _stored(db, did)["opposing_parties"][0]
    assert entry["avocat_id"] == "" and entry["avocat_name"] == ""
    assert _stored(db, did)["avocat_ids"] == []


def test_update_refuses_an_unknown_lawyer_or_the_party_itself(db):
    did = _dossier(db, [JEAN])
    for avocat, message in (("inconnu", "Avocat introuvable."),
                            ("p1", "Une partie ne peut pas être son propre avocat.")):
        doc, errors, _report = dossier_model.update_dossier_party(
            did, "p1", avocat_id=avocat)
        assert doc is None and errors == [message]


def test_an_unchanged_update_writes_nothing(db):
    did = _dossier(db, [JEAN])
    before = _stored(db, did)
    db.reset_logs()

    doc, errors, report = dossier_model.update_dossier_party(
        did, "p1", roles=["demandeur"], avocat_id="")

    assert errors == [] and report["changed"] is False
    assert db.commits == [] and _stored(db, did) == before
    assert doc["etag"] == before["etag"]


def test_update_locates_the_party_or_refuses(db):
    did = _dossier(db, [JEAN], opposing=[ROY])
    cases = (
        (dict(partie_id="p9"), dossier_model.PARTY_NOT_ON_DOSSIER),
        (dict(partie_id="p1", side="opposing_parties"),
         "Cette partie ne figure pas parmi les parties adverses du dossier."),
        (dict(partie_id="p1", side="adverses"), dossier_model.PARTY_INVALID_SIDE),
        (dict(partie_id=""), "Une partie est requise."),
    )
    for kwargs, message in cases:
        pid = kwargs.pop("partie_id")
        doc, errors, _r = dossier_model.update_dossier_party(
            did, pid, roles=[], **kwargs)
        assert doc is None and errors == [message], kwargs
    doc, errors, _r = dossier_model.update_dossier_party(did, "p1")
    assert errors == [dossier_model.PARTY_NOTHING_TO_CHANGE]
    doc, errors, _r = dossier_model.update_dossier_party("nope", "p1", roles=[])
    assert errors == ["Dossier introuvable."]


def test_a_legacy_both_sides_entry_needs_its_side(db):
    did = _dossier(db, [JEAN], opposing=[ROY])
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did),
        "opposing_parties": [dict(ROY), _entry("p1", "Jean Tremblay")],
        "opposing_party_ids": ["p3", "p1"],
    })

    doc, errors, _r = dossier_model.update_dossier_party(did, "p1", roles=[])
    assert doc is None and errors == [dossier_model.PARTY_ON_BOTH_SIDES]

    _doc, errors, report = dossier_model.update_dossier_party(
        did, "p1", side="opposing_parties", roles=["mis en cause"])
    assert errors == [] and report["side"] == "opposing_parties"
    stored = _stored(db, did)
    assert stored["opposing_parties"][1]["roles"] == ["mis en cause"]
    assert stored["clients"][0]["roles"] == ["demandeur"]


def test_a_stale_update_is_refused(db):
    did = _dossier(db, [JEAN])
    doc, errors, _r = dossier_model.update_dossier_party(
        did, "p1", roles=["intimé"], expected_etag="perimee")
    assert doc is None and errors == [concurrency.STALE_ETAG_ERROR]


# ══════════════════════════════════════════════════════════════════════
# 5. remove_dossier_party
# ══════════════════════════════════════════════════════════════════════


def test_remove_detaches_the_link_journals_it_and_keeps_the_contact(db):
    _contact(db, "p2", "Marie", "Tremblay")
    did = _dossier(db, [JEAN, MARIE])

    doc, errors, report = dossier_model.remove_dossier_party(did, "p2")

    assert errors == [] and report["changed"] is True
    assert _stored(db, did)["client_ids"] == ["p1"]
    assert report["journaled"] is True and report["was_first_client"] is False
    (row,) = _audit_rows(db)
    assert row["entity_type"] == "dossier_party" and row["entity_id"] == "p2"
    assert db.peek("parties/p2") is not None  # the contact stays


def test_removing_the_first_client_reports_the_role_it_derived(db):
    did = _dossier(db, [JEAN, _entry("p2", "Marie Tremblay", ("intimé",))])

    _doc, errors, report = dossier_model.remove_dossier_party(did, "p1")

    assert errors == []
    assert report["was_first_client"] is True
    assert (report["role_before"], report["role_after"]) == ("demandeur", "intimé")


def test_the_last_client_cannot_leave(db):
    did = _dossier(db, [JEAN], opposing=[ROY])
    before = _stored(db, did)

    doc, errors, _r = dossier_model.remove_dossier_party(did, "p1")

    assert doc is None and errors == [dossier_model.PARTY_LAST_CLIENT]
    assert _stored(db, did) == before and _audit_rows(db) == []


def test_a_served_party_cannot_be_removed_even_when_superseded(db):
    did = _dossier(db, [JEAN], opposing=[ROY])
    _doc, errors = dossier_model.update_dossier(did, {"significations": [
        {"id": "s1", "partie_id": "p3", "date": "2026-03-01",
         "mode": "huissier", "superseded_by": "s2"},
        {"id": "s2", "partie_id": "p3", "date": "2026-03-02",
         "mode": "huissier"},
    ]})
    assert errors == []

    doc, errors, _r = dossier_model.remove_dossier_party(did, "p3")

    assert doc is None
    assert errors == ["Cette partie a reçu 2 significations au dossier : une "
                      "partie signifiée ne peut pas en être retirée."]


def test_a_served_legacy_duplicate_can_lose_its_other_side(db):
    """The party stays on the dossier (the other side), so the signification
    still names a party: removing the duplicate is its repair."""
    did = _dossier(db, [JEAN], opposing=[ROY])
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did),
        "clients": [dict(JEAN), _entry("p3", "Paul Roy")],
        "client_ids": ["p1", "p3"],
        "significations": [{"id": "s1", "partie_id": "p3", "date": DT,
                            "mode": "huissier", "superseded_by": "",
                            "huissier_id": "", "pv_document_id": "",
                            "confirmee": False}],
    })

    _doc, errors, report = dossier_model.remove_dossier_party(
        did, "p3", side="clients")

    assert errors == [], errors
    assert _stored(db, did)["client_ids"] == ["p1"]
    assert _stored(db, did)["opposing_party_ids"] == ["p3"]


@pytest.mark.parametrize("history", ["key", "row"])
def test_a_client_with_trust_history_cannot_be_removed(db, history):
    did = _dossier(db, [JEAN, MARIE])
    if history == "key":
        db.external_write(f"dossiers/{did}", {
            **_stored(db, did), "trust_cleared_by_client": {"p2": 0}})
    else:
        db.seed("trust_transactions/t1", {
            "id": "t1", "dossier_id": did, "client_id": "p2", "sequence": 3})

    doc, errors, _r = dossier_model.remove_dossier_party(did, "p2")

    assert doc is None and "fidéicommis" in errors[0]
    assert len(_stored(db, did)["clients"]) == 2


def test_a_journal_failure_never_fails_the_detach(db, monkeypatch):
    did = _dossier(db, [JEAN, MARIE])

    def boom(*_a, **_k):
        raise RuntimeError("journal down")

    monkeypatch.setattr(audit_event_model, "record_deletion", boom)
    doc, errors, report = dossier_model.remove_dossier_party(did, "p2")

    assert errors == [] and report["changed"] is True
    assert report["journaled"] is False
    assert _stored(db, did)["client_ids"] == ["p1"]


def test_the_journal_row_is_written_only_after_the_commit(db):
    did = _dossier(db, [JEAN, MARIE])
    db.reset_logs()

    dossier_model.remove_dossier_party(did, "p2")

    kinds = [path.split("/")[0] for c in db.commits for _op, path in c.ops]
    assert kinds == ["dossiers", "audit_events"]


def test_a_refused_detach_journals_nothing(db):
    did = _dossier(db, [JEAN])
    dossier_model.remove_dossier_party(did, "p1")
    assert _audit_rows(db) == []


# ══════════════════════════════════════════════════════════════════════
# 6. refresh_party_names
# ══════════════════════════════════════════════════════════════════════


def test_refresh_resnapshots_names_and_is_idempotent(db):
    _contact(db, "p1", "Jean-Marc", "Tremblay")
    _contact(db, "p3", "Paul", "Roy", contact_role="partie_adverse")
    _contact(db, "av1", "Anne", "Roy-Gagnon", prefix="Me",
             contact_role="avocat_adverse")
    did = _dossier(db, [JEAN], opposing=[
        _entry("p3", "Paul Roy", ("défendeur",), "av1", "Me Anne Roy")])

    rows, errors = dossier_model.refresh_party_names(dossier_id=did)

    assert errors == []
    (row,) = rows
    assert row["outcome"] == "applied" and row["missing_partie_ids"] == []
    assert sorted((c["partie_id"], c["field"], c["before"], c["after"])
                  for c in row["changes"]) == [
        ("av1", "avocat_name", "Me Anne Roy", "Me Anne Roy-Gagnon"),
        ("p1", "name", "Jean Tremblay", "Jean-Marc Tremblay"),
    ]
    stored = _stored(db, did)
    assert stored["clients"][0]["name"] == "Jean-Marc Tremblay"
    assert stored["opposing_parties"][0]["avocat_name"] == "Me Anne Roy-Gagnon"

    db.reset_logs()
    rows, errors = dossier_model.refresh_party_names(dossier_id=did)
    assert errors == [] and rows[0]["outcome"] == "unchanged"
    assert db.commits == []


def test_each_refresh_row_carries_the_dossier_s_etag_as_stored(db):
    """Lot 4b: the connector hands each row's etag back, so its next edit of
    that dossier needs no re-read — the WRITTEN one on « applied », the
    stored one otherwise (never the pre-write etag of an applied row)."""
    _contact(db, "p1", "Jean-Marc", "Tremblay")
    did = _dossier(db, [JEAN])
    before = _stored(db, did)["etag"]

    (row,), _ = dossier_model.refresh_party_names(dossier_id=did)
    assert row["outcome"] == "applied"
    assert row["dossier_etag"] == _stored(db, did)["etag"] != before

    (row,), _ = dossier_model.refresh_party_names(dossier_id=did)
    assert row["outcome"] == "unchanged"
    assert row["dossier_etag"] == _stored(db, did)["etag"]


def test_refresh_never_blanks_a_missing_contact(db):
    _contact(db, "p1", "Jean", "Tremblay")
    did = _dossier(db, [JEAN], opposing=[ROY])  # p3 has no fiche

    rows, errors = dossier_model.refresh_party_names(dossier_id=did)

    assert errors == []
    assert rows[0]["missing_partie_ids"] == ["p3"]
    assert _stored(db, did)["opposing_parties"][0]["name"] == "Paul Roy"


def test_refresh_refuses_when_no_contact_could_be_read(db, monkeypatch):
    """get_parties_bulk fails OPEN to {} — never « every contact vanished »."""
    did = _dossier(db, [JEAN])
    monkeypatch.setattr(partie_model, "get_parties_bulk", lambda ids: {})

    rows, errors = dossier_model.refresh_party_names(dossier_id=did)

    assert rows == [] and errors == [dossier_model.PARTY_NAMES_UNREADABLE]


def test_refresh_by_contact_covers_every_dossier_it_is_cited_in(db):
    _contact(db, "av1", "Anne", "Roy-Gagnon", prefix="Me",
             contact_role="avocat_adverse")
    d1 = _dossier(db, [JEAN], opposing=[
        _entry("p3", "Paul Roy", ("défendeur",), "av1", "Me Anne Roy")])
    d2 = _dossier(db, [_entry("p5", "Luc Roy", (), "av1", "Me Anne Roy")],
                  file_number="2026-002")

    rows, errors = dossier_model.refresh_party_names(partie_id="av1")

    assert errors == []
    assert {r["dossier_id"] for r in rows} == {d1, d2}
    assert all(r["outcome"] == "applied" for r in rows)
    # Only av1's snapshots move: p3 / p5 (no fiche) are not even read.
    assert _stored(db, d2)["clients"][0]["name"] == "Luc Roy"
    assert all(r["missing_partie_ids"] == [] for r in rows)


def test_refresh_by_contact_touches_only_that_contact_s_snapshots(db):
    """Per contact: another party's stale snapshot on the same dossier is
    NOT refreshed — the caller asked about one contact."""
    _contact(db, "p1", "Jean-Marc", "Tremblay")
    _contact(db, "av1", "Anne", "Roy-Gagnon", prefix="Me",
             contact_role="avocat_adverse")
    did = _dossier(db, [JEAN], opposing=[
        _entry("p3", "Paul Roy", ("défendeur",), "av1", "Me Anne Roy")])

    rows, errors = dossier_model.refresh_party_names(partie_id="av1")

    assert errors == []
    assert [(c["partie_id"], c["field"]) for c in rows[0]["changes"]] == [
        ("av1", "avocat_name")]
    assert rows[0]["missing_partie_ids"] == []
    stored = _stored(db, did)
    assert stored["clients"][0]["name"] == "Jean Tremblay"  # untouched
    assert stored["opposing_parties"][0]["avocat_name"] == "Me Anne Roy-Gagnon"


def test_refresh_a_stale_dossier_is_refused_and_the_others_go_on(db, monkeypatch):
    _contact(db, "p1", "Jean-Marc", "Tremblay")
    d1 = _dossier(db, [JEAN])
    d2 = _dossier(db, [JEAN], file_number="2026-002")
    real = dossier_model.get_dossier

    def racing(doc_id):
        doc = real(doc_id)
        if doc_id == d1:
            db.external_write(f"dossiers/{d1}", {**db.peek(f"dossiers/{d1}"),
                                                 "etag": "e-rival"})
        return doc

    monkeypatch.setattr(dossier_model, "get_dossier", racing)
    rows, errors = dossier_model.refresh_party_names(partie_id="p1")

    assert errors == []
    by_id = {r["dossier_id"]: r for r in rows}
    assert by_id[d1]["outcome"] == "refused"
    assert by_id[d1]["reason"] == concurrency.STALE_ETAG_ERROR
    assert by_id[d2]["outcome"] == "applied"


def test_refresh_selector_rules(db, monkeypatch):
    assert dossier_model.refresh_party_names()[1]
    assert dossier_model.refresh_party_names(dossier_id="d", partie_id="p")[1]
    assert dossier_model.refresh_party_names(dossier_id="nope")[1] == [
        "Dossier introuvable."]
    assert dossier_model.refresh_party_names(partie_id="nope")[1] == [
        "Contact introuvable."]
    # A known contact on no dossier: nothing to refresh, no refusal.
    _contact(db, "p9", "Solo", "Contact")
    assert dossier_model.refresh_party_names(partie_id="p9") == ([], [])


def test_refresh_by_contact_is_bounded(db, monkeypatch):
    _contact(db, "p1", "Jean", "Tremblay")
    monkeypatch.setattr(dossier_model, "list_dossiers_for_partie_strict",
                        lambda pid: [{"id": f"d{i}"} for i in range(51)])
    rows, errors = dossier_model.refresh_party_names(partie_id="p1")
    assert rows == [] and "plus de 50" in errors[0]


def test_refresh_by_contact_refuses_when_its_dossiers_are_unreadable(
    db, monkeypatch,
):
    _contact(db, "p1", "Jean", "Tremblay")
    _fail_queries_on(monkeypatch, db, "dossiers")
    rows, errors = dossier_model.refresh_party_names(partie_id="p1")
    assert rows == [] and errors == [dossier_model.PARTY_DOSSIERS_UNREADABLE]


def test_refresh_never_touches_an_invoice(db):
    _contact(db, "p1", "Jean-Marc", "Tremblay")
    did = _dossier(db, [JEAN])
    db.seed("invoices/i1", {"id": "i1", "dossier_id": did,
                            "client_id": "p1", "client_name": "Jean Tremblay"})

    dossier_model.refresh_party_names(dossier_id=did)

    assert db.peek("invoices/i1")["client_name"] == "Jean Tremblay"


# ══════════════════════════════════════════════════════════════════════
# 7. Le lecteur strict des dossiers d'un contact
# ══════════════════════════════════════════════════════════════════════


def test_the_strict_lister_propagates_and_the_display_one_does_not(
    db, monkeypatch,
):
    did = _dossier(db, [JEAN])
    assert [d["id"] for d in dossier_model.list_dossiers_for_partie_strict("p1")] == [did]
    _fail_queries_on(monkeypatch, db, "dossiers")
    with pytest.raises(gexc.ServiceUnavailable):
        dossier_model.list_dossiers_for_partie_strict("p1")
    assert dossier_model.list_dossiers_for_partie("p1") == []


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_the_strict_lister_refuses_a_blank_id(db, blank):
    with pytest.raises(ValueError):
        dossier_model.list_dossiers_for_partie_strict(blank)


# ══════════════════════════════════════════════════════════════════════
# 8. Le vocabulaire du journal ↔ l'énumération du connecteur
# ══════════════════════════════════════════════════════════════════════


def test_the_two_link_types_are_journal_types_and_connector_filters():
    import mcp.tools as tools

    enum = tools.TOOLS["list_deletions"]["input_schema"]["properties"][
        "entity_type"]["enum"]
    for kind in ("dossier_party", "mandataire"):
        assert kind in audit_event_model.VALID_ENTITY_TYPES
        assert kind in enum


def test_the_filter_says_what_a_mandataire_row_s_title_names():
    """A `mandataire` row's title is the REPRESENTED contact, its entity_id
    the mandataire: read like any other row (« entity X titled Y »), it
    would name the wrong person. The description must say so (it did not
    on f5033ec)."""
    import mcp.tools as tools

    text = tools.TOOLS["list_deletions"]["input_schema"]["properties"][
        "entity_type"]["description"]
    assert "title = the contact it REPRESENTED" in text
    assert "entity_id = the mandataire" in text
    assert "status = the side it left" in text


# ══════════════════════════════════════════════════════════════════════
# 9. Le formulaire web rencontre les mêmes règles (re-rendu à 200)
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def web(monkeypatch):
    """The dossier routes over the shared fake store (the harness of
    tests/test_dossier_status_route.py)."""
    with mock.patch("google.cloud.firestore.Client"):
        import dav.sync as dav_sync  # noqa: F401
        import routes.doc_templates as doc_templates_routes
        import routes.documents as documents_routes
        import routes.dossiers as dossiers_routes
        import routes.hearings as hearings_routes
        import routes.invoices as invoices_routes
        import routes.notes as notes_routes
        import routes.parties as parties_routes
        import routes.protocols as protocols_routes
        import routes.reception as reception_routes
        import routes.tasks as tasks_routes
        import routes.time_expenses as time_expenses_routes
    from flask import Flask

    from tz import to_mtl
    from utils.format_fr import format_cents_fr
    from utils.icons import ms
    from utils.markdown_docx import markdown_to_safe_html
    from utils.validators import format_phone_display

    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n == "dav.sync")
               and getattr(m, "db", None) is not None]
    db = install(monkeypatch, *modules)
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v, markdown=markdown_to_safe_html,
    )
    for bp in (parties_routes.parties_bp, dossiers_routes.dossiers_bp,
               time_expenses_routes.time_expenses_bp,
               documents_routes.documents_bp, notes_routes.notes_bp,
               tasks_routes.tasks_bp, protocols_routes.protocols_bp,
               hearings_routes.hearings_bp,
               doc_templates_routes.doc_templates_bp,
               invoices_routes.invoices_bp, reception_routes.reception_bp):
        app.register_blueprint(bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client, db


def test_the_web_form_removing_a_trust_client_re_renders_with_the_reason(web):
    client, db = web
    did = _dossier(db, [JEAN, MARIE])
    db.external_write(f"dossiers/{did}", {
        **_stored(db, did), "trust_balance_by_client": {"p2": 12500}})
    stored = _stored(db, did)

    resp = client.post(f"/dossiers/{did}", data={
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients_json": ('[{"id": "p1", "name": "Jean Tremblay", '
                         '"roles": ["demandeur"]}]'),
        "status": "actif", "forum_type": "judiciaire",
        "mandate_type": "judiciaire", "fee_type": "hourly",
        "expected_etag": stored["etag"],
    })

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Marie Tremblay a eu des sommes en fidéicommis" in html
    assert _stored(db, did)["client_ids"] == ["p1", "p2"]


# ══════════════════════════════════════════════════════════════════════
# 10. Le connecteur rencontre la règle du modèle (sans surface nouvelle)
# ══════════════════════════════════════════════════════════════════════


def test_the_connector_cannot_add_an_opposing_party_as_a_client(monkeypatch):
    """``update_dossier`` checked only the SAME side (``add_clients`` of a
    contact already a client); the other side went through. The model now
    refuses it for every caller — the handler reports its words."""
    with mock.patch("google.cloud.firestore.Client"):
        import dav.sync as dav_sync
        import mcp.handlers as handlers
        import mcp.tools as tools
        import mcp.write_support as write_support
    db = install(monkeypatch, dossier_model, partie_model, trust_model,
                 audit_event_model, dav_sync, write_support)
    _contact(db, "p1", "Jean", "Tremblay")
    _contact(db, "p3", "Paul", "Roy", contact_role="partie_adverse")
    did = _dossier(db, [JEAN], opposing=[ROY])
    before = _stored(db, did)

    with pytest.raises(tools.ToolArgumentError,
                       match="à la fois client et partie adverse"):
        handlers.update_dossier({"dossier_id": did,
                                 "add_clients": [{"partie_id": "p3"}]})

    assert _stored(db, did) == before
