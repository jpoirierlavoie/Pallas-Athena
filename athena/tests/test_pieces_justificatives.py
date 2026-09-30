"""La copie d'une pièce justificative au dossier (2026-09-30).

Une DÉPENSE du compte d'administration, liée à un dossier et munie d'une
pièce justificative, en verse une COPIE parmi les documents du dossier, sous
« Mandat › Déboursés » (le dossier système de rôle ``debourses``) — l'original
reste au niveau du cabinet, sur l'écriture. ``services/pieces_justificatives``
porte la règle ; ``routes/admin_ledger`` l'atteint de deux portes : la
finalisation du reçu (``api_recu``) et « Verser une copie au dossier »
(``recu_verser``).

Tout passe ici par les VRAIS modèles (``admin_ledger``, ``document``,
``folder``, ``dossier``) au-dessus du faux Firestore partagé et du faux Cloud
Storage : ce qu'on relit est ce que le MAGASIN et le SEAU contiennent.

Familles :

1. **Le modèle** — ``attach_receipt`` écrit ``receipt_md5`` dans la même
   écriture ; ``ingest_blob_as_document(from_admin_receipt=…)`` le lien de la
   copie, validé comme celui d'une note d'honoraires, jamais par les
   métadonnées ; ``find_receipt_copies`` est STRICT.
2. **Le service** — la copie classée par RÔLE ; l'uid refusé AVANT tout
   dossier ; un échec de dossier système refuse sans rien verser à la racine ;
   une copie déjà versée (même dossier, même reçu) est RENDUE ; un même
   identifiant réservé ne donne qu'UN document ; le reçu du cabinet n'est
   jamais supprimé ; tout sauf une dépense DEBOUT est inadmissible (une
   dépense contre-passée ou annulée non plus) ; les deux refus durables
   (dossier introuvable, reçu modifié) portent leur motif ; la copie d'un
   reçu REMPLACÉ restée au dossier est signalée, jamais supprimée.
3. **Les routes** — ``api_recu`` verse la copie après avoir joint le reçu, et
   un échec de la copie reste un succès du reçu (un bandeau fermé) ; un
   succès rend lui aussi l'adresse de la fiche, SANS bandeau, pour qu'un
   bandeau périmé ne survive pas ; ``recu_verser`` (POST seulement, codes
   fermés — les deux refus durables ont le leur) ; la fiche n'offre le bouton
   qu'à une dépense admissible sans copie du reçu COURANT et dont le dossier
   existe, et signale la copie d'un reçu remplacé.
"""

import ast
import base64
import hashlib
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.admin_ledger as al
    import models.document as document_model
    import models.dossier as dossier_model  # its db is faked
    import routes.admin_ledger as ra
    import services.pieces_justificatives as pj
    from models import folder as folder_model

from flask import Blueprint, Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dossier_model,)

UTC = timezone.utc
UID = "u1"
TX = "t1"
RECU = f"users/{UID}/administration/{TX}/recu.pdf"
PDF = b"%PDF-1.7\n" + b"facture fournisseur " * 20
PDF2 = b"%PDF-1.7\n" + b"autre facture " * 30
DOC_ID = "3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b"
DOC_ID2 = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"


def _md5(data: bytes) -> str:
    return base64.b64encode(
        hashlib.md5(data, usedforsecurity=False).digest()).decode()


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _entry(**over) -> dict:
    doc = {
        "id": TX, "account_id": "ops1", "sequence": 7,
        "date": datetime(2026, 9, 12, tzinfo=UTC),
        "direction": "déboursé", "kind": "dépense", "amount": 11498,
        "net_amount": 10000, "gst_amount": 500, "qst_amount": 998,
        "category": "fournitures", "counterparty": "Transcriptions X",
        "description": "", "reference": "", "supplier_invoice_ref": "F-1",
        "method": "virement", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "Tremblay c. Lavoie",
        "invoice_id": None, "invoice_number": "", "trust_transaction_id": None,
        "receipt_storage_path": RECU, "receipt_filename": "Reçu Staples.pdf",
        "receipt_file_type": "application/pdf",
        "receipt_file_size": len(PDF), "receipt_md5": _md5(PDF),
        "status": "en_circulation", "cleared_date": None,
        "reconciliation_id": None, "reverses_id": None, "reversed_by_id": None,
        "related_transaction_id": None, "revisions": [],
        "created_at": datetime(2026, 9, 12, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 12, tzinfo=UTC), "etag": "e0",
    }
    doc.update(over)
    return doc


@pytest.fixture
def store(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(document_model.storage, "bucket", lambda: bucket)
    for did, number in (("d1", "2026-001"), ("d2", "2025-009")):
        db.seed(f"dossiers/{did}", {
            "id": did, "file_number": number, "title": "Tremblay c. Lavoie",
            "clients": [], "client_ids": [], "opposing_parties": [],
            "opposing_party_ids": [], "status": "actif",
        })
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())
    bucket.put(RECU, PDF, content_type="application/pdf",
               content_disposition="attachment")
    db.reset_logs()
    return db, bucket


@pytest.fixture
def events(monkeypatch):
    seen: list = []
    monkeypatch.setattr(pj, "log_admin_ledger_event",
                        lambda event, outcome="success", **kw:
                        seen.append((event, outcome, kw)))
    return seen


def _documents(db) -> dict:
    return db.peek_collection("documents")


def _folders(db) -> dict:
    return db.peek_collection(folder_model.COLLECTION)


def _verser(document_id=DOC_ID, uid=UID):
    return pj.verser_recu_au_dossier(TX, uid, document_id=document_id)


# ══════════════════════════════════════════════════════════════════════
# 1. Le modèle
# ══════════════════════════════════════════════════════════════════════


def test_attach_receipt_writes_the_md5_in_the_same_update(store):
    db, _bucket = store
    db.reset_logs()
    merged, errors = al.attach_receipt(
        TX, RECU, "recu.pdf", "application/pdf", len(PDF2), md5=_md5(PDF2))
    assert errors == []
    stored = db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")
    assert stored["receipt_md5"] == _md5(PDF2)
    # ONE write, the path and the digest together.
    ops = [op for c in db.commits for op in c.ops]
    assert len(db.commits) == 1 and len(ops) == 1
    assert merged["receipt_md5"] == _md5(PDF2)


def test_a_replacement_given_no_md5_never_keeps_the_replaced_one(store):
    db, _bucket = store
    al.attach_receipt(TX, RECU, "recu.pdf", "application/pdf", 10)
    assert db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")["receipt_md5"] == ""
    al.attach_receipt(TX, RECU, "recu.pdf", "application/pdf", 10,
                      md5=object())            # not a digest
    assert db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")["receipt_md5"] == ""


@pytest.mark.parametrize("link", [
    {"transaction_id": TX},                                   # a key missing
    {"transaction_id": TX, "md5": _md5(PDF), "extra": 1},     # a key too many
    {"transaction_id": "a/b", "md5": _md5(PDF)},              # a deeper path
    {"transaction_id": "", "md5": _md5(PDF)},
    {"transaction_id": TX, "md5": "pas-un-md5"},
    {"transaction_id": TX, "md5": base64.b64encode(b"x" * 20).decode()},
    "t1",
])
def test_a_malformed_receipt_link_is_refused(store, link):
    record, errors = document_model._prepare_document_record(
        "d1", "2026-001", "recu.pdf", ".pdf", "application/pdf", 10, {}, UID,
        from_admin_receipt=link)
    assert record is None
    assert errors == ["Provenance de la pièce justificative invalide."]


def test_the_receipt_link_is_stored_only_through_its_keyword(store):
    record, errors = document_model._prepare_document_record(
        "d1", "2026-001", "recu.pdf", ".pdf", "application/pdf", 10,
        {"source_admin_transaction_id": "forgé", "source_receipt_md5": "x"}, UID)
    assert errors == []
    assert "source_admin_transaction_id" not in record
    assert "source_receipt_md5" not in record
    record, errors = document_model._prepare_document_record(
        "d1", "2026-001", "recu.pdf", ".pdf", "application/pdf", 10, {}, UID,
        from_admin_receipt={"transaction_id": TX, "md5": _md5(PDF)})
    assert errors == []
    assert record["source_admin_transaction_id"] == TX
    assert record["source_receipt_md5"] == _md5(PDF)


def test_find_receipt_copies_is_strict(store, monkeypatch):
    db, _bucket = store
    assert document_model.find_receipt_copies("a/b") == []

    class _Boom:
        def collection(self, *_a, **_k):
            raise RuntimeError("store down")

    monkeypatch.setattr(document_model, "db", _Boom())
    with pytest.raises(RuntimeError):
        document_model.find_receipt_copies(TX)


# ══════════════════════════════════════════════════════════════════════
# 2. Le service
# ══════════════════════════════════════════════════════════════════════


def test_the_copy_is_filed_in_mandat_deboursees_and_the_original_stays(
        store, events):
    db, bucket = store

    result = _verser()

    assert result.code == pj.VERSEE and result.errors == []
    (doc_id, stored), = _documents(db).items()
    assert doc_id == DOC_ID == result.document["id"]
    folder = db.peek(f"{folder_model.COLLECTION}/{stored['folder_id']}")
    assert folder["system_role"] == folder_model.SYSTEM_ROLE_DEBOURSES
    parent = db.peek(f"{folder_model.COLLECTION}/{folder['parent_folder_id']}")
    assert parent["system_role"] == folder_model.SYSTEM_ROLE_MANDAT
    assert parent["parent_folder_id"] is None
    assert stored["category"] == "déboursé"
    assert stored["category_source"] == "juriste"
    assert stored["category_set_by_lawyer"] is False
    assert stored["tags"] == ["pièce_justificative"]
    assert stored["source_admin_transaction_id"] == TX
    assert stored["source_receipt_md5"] == _md5(PDF)
    assert stored["genere_depuis"] == (
        "Pièce justificative de l'écriture d'administration n° 7")
    assert "Transcriptions" not in stored["genere_depuis"]
    assert stored["document_date"].date().isoformat() == "2026-09-12"
    assert stored["display_name"] == "Reçu Staples"
    assert stored["dossier_file_number"] == "2026-001"
    assert stored["storage_path"].startswith(
        f"users/{UID}/dossiers/d1/documents/{DOC_ID}/")
    # The copy holds the receipt's bytes; the firm-level original is intact.
    assert bucket.objects[stored["storage_path"]].data == PDF
    assert bucket.objects[RECU].data == PDF
    # No register write: the link lives on the document.
    written = {path for c in db.commits for _kind, path in c.ops}
    assert not any(p.startswith(f"{al.TRANSACTIONS_COLLECTION}/") for p in written)
    assert db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")["etag"] == "e0"
    assert events == [("admin_receipt_filed", "success", {
        "transaction_id": TX, "account_id": "ops1", "dossier_id": "d1",
        "document_id": DOC_ID,
    })]


@pytest.mark.parametrize("bad", ["", "unknown", None, "a/b"])
def test_an_unusable_uid_refuses_before_any_folder_or_read(
        store, monkeypatch, bad):
    db, _bucket = store
    monkeypatch.setattr(pj, "ensure_system_folder",
                        lambda *a, **k: pytest.fail("dossier système touché"))
    db.reset_logs()

    result = _verser(uid=bad)

    assert result.document is None and result.code == pj.REFUSEE
    assert result.errors and "aucun fichier" in result.errors[0]
    assert db.reads == []            # the uid is the FIRST thing judged
    assert _folders(db) == {} and _documents(db) == {}


def test_a_system_folder_failure_refuses_and_files_nothing_at_the_root(
        store, monkeypatch, events):
    db, bucket = store
    monkeypatch.setattr(pj, "ensure_system_folder",
                        lambda dossier_id, role: (None, ["Dossier bloqué."]))
    monkeypatch.setattr(pj, "ingest_blob_as_document",
                        lambda *a, **k: pytest.fail("versé sans dossier système"))
    objects = set(bucket.objects)

    result = _verser()

    assert result.document is None and result.errors == ["Dossier bloqué."]
    assert _documents(db) == {}
    assert set(bucket.objects) == objects
    assert events == [("admin_receipt_filed", "refused",
                       {"transaction_id": TX, "reason": "dossier_systeme"})]


def test_a_copy_already_filed_is_returned_and_nothing_is_written(store, monkeypatch):
    db, bucket = store
    first = _verser()
    commits = len(db.commits)
    objects = dict(bucket.objects)
    monkeypatch.setattr(pj, "ingest_blob_as_document",
                        lambda *a, **k: pytest.fail("seconde copie"))
    monkeypatch.setattr(pj, "ensure_system_folder",
                        lambda *a, **k: pytest.fail("dossier touché"))

    again = _verser(document_id=DOC_ID2)

    assert again.code == pj.DEJA
    assert again.document["id"] == first.document["id"]
    assert len(db.commits) == commits
    assert bucket.objects == objects
    assert len(_documents(db)) == 1


def test_two_calls_with_one_document_id_make_one_document(store, monkeypatch):
    """Even when the second call cannot see the first copy (a read racing
    its commit), the RESERVED id lands both on ONE document."""
    db, bucket = store
    first = _verser()
    monkeypatch.setattr(pj, "find_receipt_copies", lambda tx_id: [])

    again = _verser()

    assert again.document["id"] == first.document["id"] == DOC_ID
    assert len(_documents(db)) == 1
    copies = [n for n in bucket.objects if n.startswith(f"users/{UID}/dossiers/")]
    assert len(copies) == 1
    assert bucket.objects[RECU].data == PDF


# A reversed dépense: an uncleared one → both legs « annulée »; a cleared one
# stays « compensée » beside its reversal (reversed_by_id). Neither is a
# disbursement of the dossier any more.
_CANCELLED = [
    {"reversed_by_id": "t2", "status": "annulée"},
    {"reversed_by_id": "t2", "status": "compensée"},
    {"status": "annulée"},
]


@pytest.mark.parametrize("over", [
    {"kind": "encaissement_facture", "direction": "recette"},
    {"kind": "recette_autre", "direction": "recette"},
    {"kind": "correction"},
    {"dossier_id": None, "dossier_file_number": ""},
    {"dossier_id": ""},
    {"receipt_storage_path": None},
    *_CANCELLED,
])
def test_only_a_depense_of_a_dossier_with_a_receipt_is_admissible(
        store, monkeypatch, over):
    db, bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(**over))
    monkeypatch.setattr(pj, "ensure_system_folder",
                        lambda *a, **k: pytest.fail("dossier système touché"))

    result = _verser()

    assert result.code == pj.INADMISSIBLE and result.document is None
    assert result.errors == [pj.MSG_INADMISSIBLE]
    assert _documents(db) == {} and _folders(db) == {}
    assert pj.admissible(_entry(**over)) is False
    assert bucket.objects[RECU].data == PDF


def test_the_admissibility_predicate():
    assert pj.admissible(_entry()) is True
    assert pj.admissible(None) is False
    assert pj.admissible({}) is False
    # Standing, cleared or not: still admissible.
    assert pj.admissible(_entry(status="compensée")) is True
    assert pj.admissible(_entry(reversed_by_id="")) is True
    for over in _CANCELLED:
        assert pj.admissible(_entry(**over)) is False, over


def test_a_receipt_attached_before_the_md5_is_judged_on_its_own_digest(store):
    db, _bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(receipt_md5=None))

    result = _verser()

    assert result.code == pj.VERSEE
    assert _documents(db)[DOC_ID]["source_receipt_md5"] == _md5(PDF)


def test_a_replaced_receipt_gets_its_own_copy_and_the_old_one_stays(store):
    db, bucket = store
    _verser()
    bucket.put(RECU, PDF2, content_type="application/pdf")
    al.attach_receipt(TX, RECU, "recu.pdf", "application/pdf", len(PDF2),
                      md5=_md5(PDF2))

    result = _verser(document_id=DOC_ID2)

    assert result.code == pj.VERSEE
    docs = _documents(db)
    assert set(docs) == {DOC_ID, DOC_ID2}
    assert docs[DOC_ID2]["source_receipt_md5"] == _md5(PDF2)
    entry = db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")
    state = pj.etat_des_copies(entry)
    assert state["courante"]["id"] == DOC_ID2


def test_a_replaced_receipts_copy_still_in_the_dossier_is_listed(store):
    """A copy of a receipt since REPLACED stays among the dossier's
    documents: it is LISTED (one per document), never deleted."""
    db, bucket = store
    _verser()
    bucket.put(RECU, PDF2, content_type="application/pdf")
    al.attach_receipt(TX, RECU, "recu.pdf", "application/pdf", len(PDF2),
                      md5=_md5(PDF2))
    entry = db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")

    state = pj.etat_des_copies(entry)
    assert state["courante"] is None
    assert state["remplacees"] == [{"document_id": DOC_ID,
                                    "dossier_file_number": "2026-001"}]

    _verser(document_id=DOC_ID2)
    state = pj.etat_des_copies(entry)
    assert state["courante"]["id"] == DOC_ID2
    assert state["remplacees"] == [{"document_id": DOC_ID,
                                    "dossier_file_number": "2026-001"}]
    assert set(_documents(db)) == {DOC_ID, DOC_ID2}      # nothing deleted

    # Only the entry's OWN dossier: once the entry moved, both copies read
    # as copies of another dossier, none as a replaced one.
    moved = _entry(dossier_id="d2", dossier_file_number="2025-009",
                   receipt_md5=_md5(PDF2))
    state = pj.etat_des_copies(moved)
    assert state["remplacees"] == []
    assert state["autres"] == [{"document_id": DOC_ID2,
                                "dossier_file_number": "2026-001"}]


def test_no_copy_reads_replaced_without_the_receipts_md5(store):
    """A receipt attached before ``receipt_md5`` existed has not been
    replaced since (a replacement writes the field): every copy is the
    current one's, none is listed as replaced."""
    db, _bucket = store
    _verser()
    state = pj.etat_des_copies(_entry(receipt_md5=None))
    assert state["courante"]["id"] == DOC_ID
    assert state["remplacees"] == []
    no_receipt = pj.etat_des_copies(_entry(receipt_storage_path=None))
    assert no_receipt["remplacees"] == []


def test_dossier_introuvable_says_true_only_on_an_answered_absence(
        store, monkeypatch):
    assert pj.dossier_introuvable(_entry(dossier_id="dx")) is True
    assert pj.dossier_introuvable(_entry()) is False
    assert pj.dossier_introuvable(_entry(dossier_id=None)) is False

    def _boom(dossier_id):
        raise RuntimeError("down")

    monkeypatch.setattr(pj, "get_dossier_strict", _boom)
    assert pj.dossier_introuvable(_entry(dossier_id="dx")) is False   # unknown


def test_a_receipt_changed_under_the_call_is_refused(store):
    db, bucket = store
    bucket.put(RECU, PDF2, content_type="application/pdf")   # md5 not updated

    result = _verser()

    assert result.document is None and result.errors == [pj.MSG_RECEIPT_CHANGED]
    # Judged BEFORE the folder: nothing written anywhere, « Mandat ›
    # Déboursés » included (it used to be created, then the copy refused).
    assert _documents(db) == {} and _folders(db) == {}


def test_an_unreadable_receipt_refuses_before_any_folder(store, monkeypatch):
    db, _bucket = store
    monkeypatch.setattr(pj, "_reload_source", lambda entry: None)

    result = _verser()

    assert result.document is None and result.reason == "recu_illisible"
    assert _documents(db) == {} and _folders(db) == {}


def test_the_lasting_refusals_carry_their_reason(store, monkeypatch, events):
    """The two refusals a retry cannot cure name themselves — the routes
    give each its own closed banner; any other refusal carries its logged
    reason (never one of the two)."""
    db, bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(dossier_id="dx"))
    gone = _verser()
    assert gone.reason == pj.REASON_DOSSIER_INTROUVABLE == "dossier_introuvable"
    assert gone.code == pj.REFUSEE

    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())
    bucket.put(RECU, PDF2, content_type="application/pdf")   # md5 not updated
    changed = _verser()
    assert changed.reason == pj.REASON_RECU_MODIFIE == "recu_modifie"
    assert "rechargez la page" in changed.errors[0]

    def _boom(dossier_id):
        raise RuntimeError("down")

    monkeypatch.setattr(pj, "get_dossier_strict", _boom)
    unreadable = _verser()
    assert unreadable.reason == "lecture_impossible"
    # The reasons on the outcome are the ones logged.
    assert [kw["reason"] for _e, outcome, kw in events if outcome == "refused"] == [
        "dossier_introuvable", "recu_modifie", "lecture_impossible"]
    assert _documents(db) == {}
    # A refusal before the entry is known admissible records no reason.
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(kind="recette_autre"))
    assert _verser().reason == ""
    assert _verser(uid="").reason == ""


def test_a_dossier_gone_or_unreadable_refuses(store, monkeypatch):
    db, _bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(dossier_id="dx"))
    assert _verser().errors == [pj.MSG_DOSSIER_NOT_FOUND]

    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())

    def _boom(dossier_id):
        raise RuntimeError("down")

    monkeypatch.setattr(pj, "get_dossier_strict", _boom)
    assert _verser().errors == [pj.MSG_READ_FAILED]
    assert _documents(db) == {} and _folders(db) == {}


def test_an_unreadable_copies_lookup_refuses_never_duplicates(store, monkeypatch):
    db, _bucket = store
    _verser()

    def _boom(tx_id):
        raise RuntimeError("down")

    monkeypatch.setattr(pj, "find_receipt_copies", _boom)
    result = _verser(document_id=DOC_ID2)
    assert result.document is None and result.errors == [pj.MSG_READ_FAILED]
    assert len(_documents(db)) == 1


def test_an_entry_moved_to_another_dossier_lists_the_old_copy(store):
    db, _bucket = store
    _verser()
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}",
            _entry(dossier_id="d2", dossier_file_number="2025-009"))
    entry = db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")

    state = pj.etat_des_copies(entry)
    assert state["courante"] is None
    assert state["autres"] == [{"document_id": DOC_ID,
                                "dossier_file_number": "2026-001"}]

    result = _verser(document_id=DOC_ID2)
    assert result.code == pj.VERSEE
    assert _documents(db)[DOC_ID2]["dossier_id"] == "d2"


def test_the_service_judges_the_uid_first_and_reaches_no_register_writer():
    """Structural: require_uid is the function's FIRST statement, and the
    service never writes to the register (the link lives on the document)."""
    source = (_ATHENA / "services" / "pieces_justificatives.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "verser_recu_au_dossier")
    first = fn.body[1] if isinstance(fn.body[0], ast.Expr) else fn.body[0]
    assert isinstance(first, ast.Try)
    call = first.body[0].value
    assert call.func.attr == "require_uid"
    writers = {"attach_receipt", "create_transaction", "update_transaction",
               "delete_transaction", "reverse_transaction", "clear_transaction"}
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & writers
    assert "delete" not in called          # the firm object is never deleted


def _imports_the_service(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "services.pieces_justificatives" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "services.pieces_justificatives":
                return True
            if node.module == "services" and any(
                    a.name == "pieces_justificatives" for a in node.names):
                return True
    return False


def test_only_the_administration_routes_import_the_service():
    importers = []
    for package in ("routes", "services", "mcp", "models", "dav", "utils",
                    "client", "scripts"):
        for path in sorted((_ATHENA / package).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if _imports_the_service(tree):
                importers.append(path.relative_to(_ATHENA).as_posix())
    assert importers == ["routes/admin_ledger.py"]


# ══════════════════════════════════════════════════════════════════════
# 3. Les routes
# ══════════════════════════════════════════════════════════════════════


def _app(*, templates: bool = False) -> Flask:
    from utils.format_fr import format_cents_fr
    from utils.html_attr import jsattr
    from utils.icons import ms as _ms

    kwargs = {"template_folder": str(_ATHENA / "templates")} if templates else {}
    app = Flask(__name__, **kwargs)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.jinja_env.filters["cents_fr"] = format_cents_fr

    app.jinja_env.filters["jsattr"] = jsattr   # the real filter, never a copy
    app.jinja_env.filters["to_mtl"] = lambda v: v
    app.register_blueprint(ra.admin_bp)
    # The two endpoints the entry card links to (their real blueprints pull
    # in half the application).
    docs = Blueprint("documents", __name__)
    docs.add_url_rule("/documents/<document_id>", "document_detail",
                      lambda document_id: "")
    dossiers = Blueprint("dossiers", __name__)
    dossiers.add_url_rule("/dossiers/<dossier_id>", "dossier_detail",
                          lambda dossier_id: "")
    app.register_blueprint(docs)
    app.register_blueprint(dossiers)
    return app


def _client(app):
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return client


@pytest.fixture
def web(store):
    return _client(_app())


@pytest.fixture
def web_rendu(store):
    return _client(_app(templates=True))


_STAGING = f"staging/{UID}/{DOC_ID}/Reçu Staples.pdf"


def _post_recu(web, bucket, name="Reçu Staples.pdf", data=PDF):
    objet = f"staging/{UID}/{DOC_ID}/{name}"
    bucket.put(objet, data, content_type="application/pdf")
    return web.post(f"/administration/{TX}/api/recu",
                    json={"objet": objet, "name": name})


def test_api_recu_attaches_then_files_the_copy_from_the_firm_blob(
        web, store, monkeypatch):
    db, bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}",
            _entry(receipt_storage_path=None, receipt_md5=None,
                   receipt_filename=""))
    bucket.remove(RECU)
    seen = {}
    real = pj.ingest_blob_as_document

    def _spy(source_blob, dossier_id, file_number, filename, metadata, user_id, **kw):
        seen.update(source=source_blob.name, dossier_id=dossier_id,
                    metadata=dict(metadata), **kw)
        return real(source_blob, dossier_id, file_number, filename, metadata,
                    user_id, **kw)

    monkeypatch.setattr(pj, "ingest_blob_as_document", _spy)

    response = _post_recu(web, bucket)

    assert response.status_code == 200
    # The entry page, built by the server — with NO banner.
    assert response.get_json() == {"ok": True, "suivant": f"/administration/{TX}"}
    firm = f"users/{UID}/administration/{TX}/Recu_Staples.pdf"
    entry = db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")
    assert entry["receipt_storage_path"] == firm
    assert entry["receipt_md5"] == _md5(PDF)
    # The copy's source is the FIRM blob, never the (consumed) staging one.
    assert seen["source"] == firm
    assert seen["document_id"] == DOC_ID
    assert seen["lawyer_set_category"] is False
    assert seen["category_source"] == "juriste"
    assert seen["metadata"]["category"] == "déboursé"
    stored = _documents(db)[DOC_ID]
    folder = db.peek(f"{folder_model.COLLECTION}/{stored['folder_id']}")
    assert folder["system_role"] == folder_model.SYSTEM_ROLE_DEBOURSES
    assert bucket.objects[firm].data == PDF          # the original stays
    assert not any(n.startswith("staging/") for n in bucket.objects)


@pytest.mark.parametrize("failure", ["refusal", "raise"])
def test_a_failed_copy_is_still_a_saved_receipt_with_a_closed_banner(
        web, store, monkeypatch, failure):
    db, bucket = store
    calls = []

    def _fail(tx_id, uid, *, document_id):
        calls.append(document_id)
        if failure == "raise":
            raise RuntimeError("boom")
        return pj.Versement(None, ["non"], pj.REFUSEE)

    monkeypatch.setattr(pj, "verser_recu_au_dossier", _fail)

    response = _post_recu(web, bucket)

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    target = urlparse(body["suivant"])
    assert target.path == f"/administration/{TX}"
    assert parse_qs(target.query) == {"avertissement": ["versement"]}
    assert calls == [DOC_ID]
    # attach_receipt DID run: the entry names the new receipt and its digest.
    entry = db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")
    assert entry["receipt_storage_path"].endswith("/Recu_Staples.pdf")
    assert entry["receipt_md5"] == _md5(PDF)


@pytest.mark.parametrize("over", [
    {"kind": "encaissement_facture", "direction": "recette"},
    {"kind": "recette_autre", "direction": "recette"},
    {"kind": "correction"},
    {"dossier_id": None},
    *_CANCELLED,
])
def test_no_copy_is_attempted_for_an_inadmissible_entry(
        web, store, monkeypatch, over):
    db, bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(**over))
    monkeypatch.setattr(pj, "verser_recu_au_dossier",
                        lambda *a, **k: pytest.fail("copie tentée"))

    response = _post_recu(web, bucket)

    assert response.status_code == 200
    # The receipt is attached (an annulée entry keeps the right to it) and
    # the page to return to carries no banner.
    assert response.get_json() == {"ok": True, "suivant": f"/administration/{TX}"}
    assert _documents(db) == {}
    assert db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")[
        "receipt_storage_path"].endswith("/Recu_Staples.pdf")


@pytest.mark.parametrize("already", [False, True])
def test_a_filed_copy_answers_the_entry_page_without_a_banner(
        web, store, already):
    """A successful upload never leaves the widget to reload the address it
    was opened from: a ?avertissement=versement an earlier failure put there
    would otherwise survive the copy that has just been filed (or found
    already filed — « déjà » is a success)."""
    db, bucket = store
    if already:
        _verser()
    body = _post_recu(web, bucket).get_json()
    assert body["ok"] is True
    target = urlparse(body["suivant"])
    assert target.path == f"/administration/{TX}"
    assert target.query == ""
    assert len(_documents(db)) == 1


@pytest.mark.parametrize("versement, code", [
    (pj.Versement(None, ["x"], pj.REFUSEE, pj.REASON_DOSSIER_INTROUVABLE),
     "versement_dossier_introuvable"),
    (pj.Versement(None, ["x"], pj.REFUSEE, pj.REASON_RECU_MODIFIE),
     "versement_piece_modifiee"),
    (pj.Versement(None, ["x"], pj.REFUSEE, "lecture_impossible"), "versement"),
    (pj.Versement(None, ["x"], pj.REFUSEE, "ingestion"), "versement"),
    (pj.Versement(None, [pj.MSG_INADMISSIBLE], pj.INADMISSIBLE),
     "versement_inadmissible"),
])
def test_api_recu_gives_a_lasting_refusal_its_own_banner(
        web, store, monkeypatch, versement, code):
    _db, bucket = store
    monkeypatch.setattr(pj, "verser_recu_au_dossier",
                        lambda *a, **k: versement)
    body = _post_recu(web, bucket).get_json()
    assert body["ok"] is True
    target = urlparse(body["suivant"])
    assert target.path == f"/administration/{TX}"
    assert parse_qs(target.query) == {"avertissement": [code]}


def test_api_recu_for_an_entry_whose_dossier_is_gone(web, store):
    """End to end on the real service: the receipt is attached, the copy is
    refused, and the banner says WHY — not « réessayez »."""
    db, bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(dossier_id="dx"))
    body = _post_recu(web, bucket).get_json()
    assert parse_qs(urlparse(body["suivant"]).query) == {
        "avertissement": ["versement_dossier_introuvable"]}
    assert _documents(db) == {} and _folders(db) == {}
    assert db.peek(f"{al.TRANSACTIONS_COLLECTION}/{TX}")[
        "receipt_storage_path"].endswith("/Recu_Staples.pdf")


def test_recu_verser_is_a_post(web):
    assert web.get(f"/administration/{TX}/recu/verser").status_code == 405


def test_recu_verser_unknown_entry_is_a_404(web_rendu):
    assert web_rendu.post("/administration/inconnue/recu/verser").status_code == 404


def _warning(response):
    assert response.status_code == 302
    target = urlparse(response.location)
    assert target.path == f"/administration/{TX}"
    return parse_qs(target.query).get("avertissement")


def test_recu_verser_files_the_copy_and_returns_to_the_entry(web, store):
    db, _bucket = store
    response = web.post(f"/administration/{TX}/recu/verser",
                        data={"document_id": DOC_ID})
    assert _warning(response) is None
    assert set(_documents(db)) == {DOC_ID}


def test_recu_verser_inadmissible_is_a_closed_code(web, store):
    db, _bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}",
            _entry(kind="recette_autre", direction="recette"))
    response = web.post(f"/administration/{TX}/recu/verser",
                        data={"document_id": DOC_ID})
    assert _warning(response) == ["versement_inadmissible"]
    assert _documents(db) == {}


@pytest.mark.parametrize("outcome, expected", [
    ("refusal", "versement"),
    ("raise", "versement"),
    ("inadmissible", "versement_inadmissible"),
    (pj.REASON_DOSSIER_INTROUVABLE, "versement_dossier_introuvable"),
    (pj.REASON_RECU_MODIFIE, "versement_piece_modifiee"),
    ("lecture_impossible", "versement"),
])
def test_recu_verser_failures_are_closed_codes(web, monkeypatch, outcome, expected):
    def _service(tx_id, uid, *, document_id):
        if outcome == "raise":
            raise RuntimeError("boom")
        if outcome == "inadmissible":
            return pj.Versement(None, [pj.MSG_INADMISSIBLE], pj.INADMISSIBLE)
        if outcome == "refusal":
            return pj.Versement(None, ["non"], pj.REFUSEE)
        return pj.Versement(None, ["non"], pj.REFUSEE, outcome)

    monkeypatch.setattr(pj, "verser_recu_au_dossier", _service)
    response = web.post(f"/administration/{TX}/recu/verser",
                        data={"document_id": DOC_ID})
    assert _warning(response) == [expected]


def test_recu_verser_lasting_refusals_end_to_end(web, store):
    db, bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(dossier_id="dx"))
    response = web.post(f"/administration/{TX}/recu/verser",
                        data={"document_id": DOC_ID})
    assert _warning(response) == ["versement_dossier_introuvable"]

    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())
    bucket.put(RECU, PDF2, content_type="application/pdf")   # md5 not updated
    response = web.post(f"/administration/{TX}/recu/verser",
                        data={"document_id": DOC_ID})
    assert _warning(response) == ["versement_piece_modifiee"]
    assert _documents(db) == {}


@pytest.mark.parametrize("over", _CANCELLED)
def test_recu_verser_refuses_a_reversed_or_cancelled_depense(web, store, over):
    db, _bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(**over))
    response = web.post(f"/administration/{TX}/recu/verser",
                        data={"document_id": DOC_ID})
    assert _warning(response) == ["versement_inadmissible"]
    assert _documents(db) == {} and _folders(db) == {}


@pytest.mark.parametrize("posted, kept", [
    (DOC_ID, True), ("pas-un-uuid", False), ("", False),
    (DOC_ID.upper(), False),
])
def test_recu_verser_passes_only_a_canonical_document_id(
        web, monkeypatch, posted, kept):
    seen = []
    monkeypatch.setattr(
        pj, "verser_recu_au_dossier",
        lambda tx_id, uid, *, document_id: (
            seen.append(document_id) or pj.Versement({"id": document_id}, [], pj.VERSEE)))
    web.post(f"/administration/{TX}/recu/verser", data={"document_id": posted})
    (document_id,) = seen
    assert document_model.is_canonical_uuid4(document_id)
    assert (document_id == posted) is kept


def _card(html: str) -> str:
    start = html.index("Pièce justificative</p>")
    return html[start:html.index('<div class="mt-4 flex flex-wrap', start)]


def test_the_entry_offers_the_button_only_when_admissible_and_not_filed(
        web_rendu, store):
    db, _bucket = store
    html = web_rendu.get(f"/administration/{TX}").get_data(as_text=True)
    card = _card(html)
    assert f'action="/administration/{TX}/recu/verser"' in card
    assert "Verser une copie au dossier" in card
    minted = re.search(r'name="document_id" value="([^"]+)"', card).group(1)
    assert document_model.is_canonical_uuid4(minted)

    _verser()
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert "Verser une copie au dossier" not in card
    assert "Copie au dossier 2026-001, sous Mandat › Déboursés" in card
    assert f'href="/documents/{DOC_ID}"' in card


def test_the_folder_is_named_only_while_the_copy_is_there(web_rendu, store):
    """The lawyer may refile the copy: the page then names the dossier and
    never a folder the copy has left."""
    db, _bucket = store
    _verser()
    other, errors = folder_model.create_folder("d1", "Ailleurs")
    assert errors == []
    db.external_write(f"documents/{DOC_ID}", {
        **db.peek(f"documents/{DOC_ID}"), "folder_id": other["id"]})
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert "Copie au dossier 2026-001" in card
    assert "Mandat › Déboursés" not in card
    assert "Verser une copie au dossier" not in card


def test_a_current_copy_is_said_even_once_the_entry_is_no_longer_a_depense(
        web_rendu, store):
    db, _bucket = store
    _verser()
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}",
            _entry(kind="recette_autre", direction="recette"))
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert "Copie au dossier 2026-001, sous Mandat › Déboursés" in card
    assert "Verser une copie au dossier" not in card


def test_no_button_for_an_inadmissible_entry(web_rendu, store):
    db, _bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}",
            _entry(kind="recette_autre", direction="recette"))
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert "Verser une copie au dossier" not in card
    assert "recu/verser" not in card


@pytest.mark.parametrize("over", _CANCELLED)
def test_no_button_for_a_reversed_or_cancelled_depense(web_rendu, store, over):
    db, _bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(**over))
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert "Verser une copie au dossier" not in card
    assert "recu/verser" not in card


_GONE = ("Le dossier de cette écriture est introuvable : aucune copie ne "
         "peut y être versée.")


def test_the_card_says_the_dossier_is_gone_instead_of_the_button(
        web_rendu, store):
    db, _bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(dossier_id="dx"))
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert "Verser une copie au dossier" not in card
    assert "recu/verser" not in card
    assert _GONE in card


def test_an_unreadable_dossier_keeps_the_button(web_rendu, store, monkeypatch):
    """Fail-open: « unknown » is not « gone » — the button stays, and its
    own strict read decides."""
    db, _bucket = store
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(dossier_id="dx"))

    def _boom(dossier_id):
        raise RuntimeError("down")

    monkeypatch.setattr(pj, "get_dossier_strict", _boom)
    response = web_rendu.get(f"/administration/{TX}")
    assert response.status_code == 200
    card = _card(response.get_data(as_text=True))
    assert "Verser une copie au dossier" in card
    assert _GONE not in card


def test_the_dossier_is_read_only_when_the_button_would_be_offered(
        web_rendu, store, monkeypatch):
    db, _bucket = store
    _verser()                                    # a current copy: no button
    monkeypatch.setattr(pj, "get_dossier_strict",
                        lambda *_a: pytest.fail("dossier relu pour rien"))
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert "Copie au dossier 2026-001" in card
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}",
            _entry(kind="recette_autre", direction="recette"))
    assert web_rendu.get(f"/administration/{TX}").status_code == 200


_REPLACED = "Une copie d'une pièce justificative remplacée demeure au dossier"


def test_a_replaced_receipts_copy_is_listed_on_the_card(web_rendu, store):
    """The lawyer finds a WRONG receipt's copy from the entry — a link to the
    document; nothing deletes it."""
    db, bucket = store
    _verser()
    bucket.put(RECU, PDF2, content_type="application/pdf")
    al.attach_receipt(TX, RECU, "recu.pdf", "application/pdf", len(PDF2),
                      md5=_md5(PDF2))

    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert f"{_REPLACED} 2026-001" in card
    assert f'href="/documents/{DOC_ID}"' in card
    assert "Verser une copie au dossier" in card      # the new receipt's copy

    _verser(document_id=DOC_ID2)
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert card.count(_REPLACED) == 1
    assert f'href="/documents/{DOC_ID}"' in card      # the replaced one
    assert f'href="/documents/{DOC_ID2}"' in card     # the current one
    assert "Verser une copie au dossier" not in card
    assert set(_documents(db)) == {DOC_ID, DOC_ID2}


def test_no_replaced_copy_is_claimed_for_the_current_receipt(web_rendu, store):
    _verser()
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert _REPLACED not in card


def test_a_copy_filed_for_the_previous_dossier_is_said(web_rendu, store):
    db, _bucket = store
    _verser()
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}",
            _entry(dossier_id="d2", dossier_file_number="2025-009"))
    card = _card(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    assert "Une copie a été versée au dossier 2026-001." in card
    assert "Verser une copie au dossier" in card        # none for 2025-009 yet


def test_an_unreadable_copies_read_claims_and_offers_nothing(
        web_rendu, store, monkeypatch):
    def _boom(tx_id):
        raise RuntimeError("down")

    monkeypatch.setattr(pj, "find_receipt_copies", _boom)
    response = web_rendu.get(f"/administration/{TX}")
    assert response.status_code == 200
    card = _card(response.get_data(as_text=True))
    assert "Verser une copie au dossier" not in card
    assert "Copie au dossier" not in card


_BANNERS = [
    ("versement", "sa copie n'a pas pu\n    être versée au dossier"),
    ("versement_inadmissible", "Seule une dépense liée à un dossier"),
    ("versement_dossier_introuvable",
     "Le dossier de cette écriture est introuvable : aucune copie ne peut y "
     "être versée. Liez l'écriture à un dossier existant."),
    ("versement_piece_modifiee",
     "La pièce justificative a changé pendant le versement — rechargez la "
     "page, puis réessayez."),
]


@pytest.mark.parametrize("code, text", _BANNERS)
def test_the_closed_banners_are_rendered(web_rendu, code, text):
    html = web_rendu.get(f"/administration/{TX}?avertissement={code}").get_data(
        as_text=True)
    assert text in html
    assert html.count('role="alert"') == 1


def test_the_lasting_banners_never_say_only_retry(web_rendu):
    """A refusal a retry cannot cure never reads as the generic
    « réessayez avec Verser une copie »."""
    for code in ("versement_dossier_introuvable", "versement_piece_modifiee"):
        html = web_rendu.get(f"/administration/{TX}?avertissement={code}").get_data(
            as_text=True)
        assert "Réessayez avec « Verser une copie au dossier »" not in html


def test_the_upload_script_follows_the_servers_address():
    source = (_ATHENA / "templates" / "administration" / "detail.html").read_text(
        encoding="utf-8")
    assert ("if (resultat.suivant) { window.location.assign(resultat.suivant); "
            "return; }") in source


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def test_the_new_markup_uses_only_compiled_classes(web_rendu, store):
    db, bucket = store
    banner_pages = [
        web_rendu.get(f"/administration/{TX}?avertissement={c}").get_data(
            as_text=True) for c, _text in _BANNERS]
    pages = list(banner_pages)
    # The dossier-gone state of the card.
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry(dossier_id="dx"))
    pages.append(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())
    _verser()
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}",
            _entry(dossier_id="d2", dossier_file_number="2025-009"))
    pages.append(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    db.seed(f"{al.TRANSACTIONS_COLLECTION}/{TX}", _entry())
    pages.append(web_rendu.get(f"/administration/{TX}").get_data(as_text=True))
    # A replaced receipt's copy listed on the card.
    bucket.put(RECU, PDF2, content_type="application/pdf")
    al.attach_receipt(TX, RECU, "recu.pdf", "application/pdf", len(PDF2),
                      md5=_md5(PDF2))
    replaced = web_rendu.get(f"/administration/{TX}").get_data(as_text=True)
    assert _REPLACED in replaced and _GONE in pages[len(_BANNERS)]
    pages.append(replaced)
    snippets = [_card(p) for p in pages]
    for page in banner_pages:
        i = page.index('role="alert"')
        snippets.append(page[i - 100:i + 400])
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    classes = {c for s in snippets
               # the static attribute only — never Alpine's `:class` binding
               for block in re.findall(r'(?<![:\w-])class="([^"]+)"', s)
               for c in block.split()}
    assert classes
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert not absent, absent
