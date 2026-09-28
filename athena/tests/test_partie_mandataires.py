"""Mandataires, une représentation à la fois — lot 4a, étape 2 (plan D6).

Le formulaire web poste la liste ``mandataires`` ENTIÈRE ; le connecteur
(lot 4b) en ajoutera, corrigera et retirera UNE. Les trois aides du modèle
(``add_partie_mandataire`` / ``update_partie_mandataire`` /
``remove_partie_mandataire``) reconstruisent la liste à partir de la liste
STOCKÉE — toute autre entrée réécrite telle quelle — et la sauvegardent par
``update_partie``, dont la règle directe (``_validate``) et la règle à
rebours (lot 0b, ``tests/test_partie_mandataire_reverse.py``) restent en
vigueur pour tous les chemins.

Deux défauts corrigés avec elles :

* les notes d'une représentation n'étaient ni assainies ni bornées —
  ``_sanitize_data`` ne visite que les chaînes de PREMIER niveau ;
* un mandataire retiré (par le formulaire comme par une aide) ne laissait
  aucune trace : chaque retrait s'inscrit désormais à ``audit_events``
  (``mandataire``), après le commit — un LIEN retiré, le contact reste.

Vrai client Firestore au-dessus du faux serveur partagé ; on relit le
STOCKÉ.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import audit_event as audit_event_model
    from models import concurrency
    from models import partie as pm

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    fake = install(monkeypatch, pm, audit_event_model)
    for pid, first, last, role, kind in (
        ("m1", "Luc", "Roy", "client", "individual"),
        ("m2", "Anne", "Gagnon", "client", "individual"),
        ("m3", "Paul", "Adverse", "partie_adverse", "individual"),
        ("org", "", "", "client", "organization"),
    ):
        fake.seed(f"parties/{pid}", {
            **pm._default_doc(), "id": pid, "type": kind,
            "contact_role": role, "first_name": first, "last_name": last,
            "organization_name": "Béton Nord inc." if kind == "organization" else "",
            "etag": f"e-{pid}", "created_at": DT, "updated_at": DT})
    doc, errors = pm.create_partie({
        "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay",
        "mandataires": [{"id": "m1", "kind": "tuteur",
                         "notes": "Jugement du 3 mars."}],
    })
    assert errors == [], errors
    fake.represented = doc["id"]
    fake.reset_logs()
    return fake


def _stored(db):
    return db.peek(f"parties/{db.represented}")


def _audit_rows(db):
    return list(db.peek_collection("audit_events").values())


# ══════════════════════════════════════════════════════════════════════
# 1. Ajouter
# ══════════════════════════════════════════════════════════════════════


def test_add_appends_to_the_stored_list(db):
    before = _stored(db)["mandataires"]

    doc, errors, report = pm.add_partie_mandataire(
        db.represented, "m2", kind="curateur", notes="Nommée le 5 mai.")

    assert errors == [] and report["changed"] is True
    assert _stored(db)["mandataires"] == before + [
        {"id": "m2", "kind": "curateur", "notes": "Nommée le 5 mai."}]
    assert report["mandataires_count"] == 2
    assert doc["etag"] == _stored(db)["etag"]


def test_an_identical_add_is_a_no_op(db):
    before = _stored(db)

    doc, errors, report = pm.add_partie_mandataire(
        db.represented, "m1", kind="tuteur", notes="Jugement du 3 mars.")

    assert errors == [] and report["changed"] is False
    assert db.commits == [] and _stored(db) == before
    assert doc["etag"] == before["etag"]


def test_an_add_that_differs_from_the_listed_entry_is_refused(db):
    doc, errors, _r = pm.add_partie_mandataire(
        db.represented, "m1", kind="curateur")
    assert doc is None and errors == [pm.MANDATAIRE_ALREADY_LISTED]
    assert db.commits == []


@pytest.mark.parametrize("mid, kind, message", [
    ("SELF", "tuteur", "Un contact ne peut pas être son propre mandataire."),
    ("m9", "tuteur", "Mandataire introuvable."),
    ("org", "tuteur", "Un mandataire doit être une personne physique."),
    ("m3", "tuteur",
     "Un mandataire doit avoir le même rôle que le contact représenté."),
    ("m2", "parrain", "Type de représentation invalide."),
    ("", "tuteur", "Le contact représenté et le mandataire sont requis."),
])
def test_add_names_each_refusal_and_writes_nothing(db, mid, kind, message):
    mid = db.represented if mid == "SELF" else mid
    before = _stored(db)

    doc, errors, report = pm.add_partie_mandataire(db.represented, mid, kind=kind)

    assert doc is None and errors == [message] and report["changed"] is False
    assert _stored(db) == before and db.commits == []


def test_add_refuses_notes_it_would_otherwise_alter(db):
    for notes, fragment in (("x" * 2001, "dépassent 2000 caractères"),
                            ("voir <b>le jugement</b>", "chevrons")):
        doc, errors, _r = pm.add_partie_mandataire(
            db.represented, "m2", kind="curateur", notes=notes)
        assert doc is None and fragment in errors[0]
        assert "le jugement" not in errors[0]  # never quotes the text
    assert db.commits == []


def test_add_refuses_an_unknown_contact_and_a_stale_version(db):
    assert pm.add_partie_mandataire("p9", "m2", kind="tuteur")[1] == [
        "Contact introuvable."]
    assert pm.add_partie_mandataire(
        db.represented, "m2", kind="tuteur", expected_etag="perimee",
    )[1] == [concurrency.STALE_ETAG_ERROR]


# ══════════════════════════════════════════════════════════════════════
# 2. Corriger
# ══════════════════════════════════════════════════════════════════════


def test_update_changes_one_entry_and_leaves_the_others(db):
    pm.add_partie_mandataire(db.represented, "m2", kind="curateur")

    _doc, errors, report = pm.update_partie_mandataire(
        db.represented, "m1", kind="mandataire")

    assert errors == [] and report["changed"] is True
    assert _stored(db)["mandataires"] == [
        {"id": "m1", "kind": "mandataire", "notes": "Jugement du 3 mars."},
        {"id": "m2", "kind": "curateur", "notes": ""},
    ]


def test_update_can_clear_the_notes_and_a_same_value_is_a_no_op(db):
    _doc, errors, report = pm.update_partie_mandataire(
        db.represented, "m1", notes="")
    assert errors == [] and report["changed"] is True
    assert _stored(db)["mandataires"][0]["notes"] == ""

    db.reset_logs()
    _doc, errors, report = pm.update_partie_mandataire(
        db.represented, "m1", kind="tuteur", notes="")
    assert errors == [] and report["changed"] is False and db.commits == []


def test_update_refuses_what_it_cannot_do(db):
    assert pm.update_partie_mandataire(db.represented, "m2", kind="tuteur")[1] == [
        pm.MANDATAIRE_NOT_LISTED]
    assert pm.update_partie_mandataire(db.represented, "m1")[1] == [
        pm.MANDATAIRE_NOTHING_TO_CHANGE]
    assert pm.update_partie_mandataire(db.represented, "m1", kind="x")[1] == [
        "Type de représentation invalide."]
    assert db.commits == []


# ══════════════════════════════════════════════════════════════════════
# 3. Retirer — un LIEN, journalisé
# ══════════════════════════════════════════════════════════════════════


def test_remove_detaches_journals_and_keeps_the_mandataire_contact(db):
    _doc, errors, report = pm.remove_partie_mandataire(db.represented, "m1")

    assert errors == [] and report["changed"] is True
    assert report["journaled"] is True and report["kind"] == "tuteur"
    assert _stored(db)["mandataires"] == []
    (row,) = _audit_rows(db)
    assert row["entity_type"] == "mandataire" and row["entity_id"] == "m1"
    assert row["snapshot_min"] == {"title": "Jean Tremblay", "status": "tuteur"}
    assert db.peek("parties/m1") is not None


def test_removing_an_unlisted_entry_is_refused(db):
    doc, errors, report = pm.remove_partie_mandataire(db.represented, "m2")
    assert doc is None and errors == [pm.MANDATAIRE_NOT_LISTED]
    assert report["journaled"] is False and _audit_rows(db) == []


def test_a_journal_failure_never_fails_the_remove(db, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("journal down")

    monkeypatch.setattr(audit_event_model, "record_deletion", boom)
    _doc, errors, report = pm.remove_partie_mandataire(db.represented, "m1")
    assert errors == [] and report["journaled"] is False
    assert _stored(db)["mandataires"] == []


def test_the_web_form_detach_is_journaled_too(db):
    """The form posts the whole list: the MODEL sees the detach."""
    _doc, errors = pm.update_partie(db.represented, {"mandataires": []})
    assert errors == []
    (row,) = _audit_rows(db)
    assert row["entity_id"] == "m1"


def test_an_edit_that_keeps_the_list_journals_nothing(db):
    _doc, errors = pm.update_partie(db.represented, {"notes": "Mémo."})
    assert errors == [] and _audit_rows(db) == []


# ══════════════════════════════════════════════════════════════════════
# 4. Les notes d'une représentation sont assainies et bornées
# ══════════════════════════════════════════════════════════════════════


def test_the_form_path_sanitizes_and_caps_each_entry_s_notes(db):
    """LE défaut : _sanitize_data ne visite que les chaînes de premier
    niveau, si bien que les notes d'une représentation étaient stockées
    sans retrait de balises ni plafond."""
    _doc, errors = pm.update_partie(db.represented, {"mandataires": [
        {"id": "m1", "kind": "tuteur", "notes": "<b>vu</b> " + "x" * 3000},
    ]})

    assert errors == []
    notes = _stored(db)["mandataires"][0]["notes"]
    assert "<b>" not in notes and len(notes) <= pm.MANDATAIRE_NOTES_MAX
