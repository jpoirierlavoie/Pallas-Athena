"""Les gabarits gardent leurs versions (lot 2A, étape T3 — décision D11).

Avant T3, remplacer le fichier d'un gabarit le DÉTRUISAIT : écrasement en
place quand le nom de fichier était le même, suppression de l'ancien objet
sinon. Le gabarit imprimé sur chaque note d'honoraires d'un client se
perdait donc d'un clic, sans retour. Et la fusion brute
``{**existing, **data}`` laissait n'importe quel appelant réécrire
``storage_path``, ``placeholders``, ``version`` ou ``id``.

Tout ici passe par le VRAI modèle, au-dessus du faux Firestore partagé (le
client est le vrai) et du faux Cloud Storage réaliste (``tests/_fake_gcs``,
qui REFUSE ce que le service refuse : une création sur un objet existant,
une suppression d'une autre génération). On relit ce qui est STOCKÉ — le
document, ses entrées ``versions/*``, les octets du seau — jamais un
dictionnaire remis à un faux.
"""

import ast
import hashlib
import io
import os
import pathlib
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.doc_template as tpl
    from models import concurrency

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

ATHENA = pathlib.Path(__file__).resolve().parent.parent
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)


def docx(text: str) -> bytes:
    """A minimal valid .docx whose body is *text* (placeholders included)."""
    body = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
        'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
        f'<w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("word/document.xml", body)
    return buf.getvalue()


V1 = docx("Premier {{tribunal}}")
V2 = docx("Deuxième {{tribunal}} {{objet_lettre}}")
V3 = docx("Troisième {{dossier.numero_cour}}")


@pytest.fixture()
def store(monkeypatch):
    db = install(monkeypatch, tpl)
    bucket = FakeBucket()
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)
    return db, bucket


def _create(data=V1, *, name="Lettre", kind="gabarit", filename="lettre.docx"):
    doc, errors = tpl.create_template(
        io.BytesIO(data), filename, len(data),
        {"name": name, "category": "correspondance", "kind": kind}, UID,
    )
    assert errors == [], errors
    return doc


def _replace(tid, data, *, filename="lettre.docx", **kw):
    return tpl.update_template(
        tid, {}, file_stream=io.BytesIO(data), filename=filename,
        file_size=len(data), **kw,
    )


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _versions(db, tid):
    return db.peek_collection(f"doc_templates/{tid}/versions")


# ══════════════════════════════════════════════════════════════════════
# 1. La création : la version 1 est une version comme les autres
# ══════════════════════════════════════════════════════════════════════


def test_a_new_template_is_version_1_at_its_own_v1_path(store):
    db, bucket = store
    doc = _create()
    tid = doc["id"]
    path = f"users/{UID}/templates/{tid}/v1/lettre.docx"
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["storage_path"] == path and stored["version"] == 1
    assert stored["sha256"] == _sha(V1)
    assert bucket.objects[path].data == V1
    entry = _versions(db, tid)["1"]
    assert entry["version"] == 1 and entry["storage_path"] == path
    assert entry["sha256"] == _sha(V1) and entry["file_size"] == len(V1)
    assert entry["filename"] == "lettre.docx"
    assert entry["placeholders"] == ["tribunal"]
    assert entry["created_via"] == "script"    # no request: the writer is a script
    assert entry["created_at"] is not None
    # Write-once, no etag (the audit_events exception to Rule 7).
    assert "etag" not in entry


def test_a_new_special_template_is_never_designated_on_creation(store):
    db, _ = store
    doc = _create(kind="note_honoraires")
    stored = db.peek(f"doc_templates/{doc['id']}")
    assert not set(tpl.ACTIVE_FIELDS) & set(stored)
    assert not tpl.is_active(stored)


RESERVED = "7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"


def _create_reserved(data=V1, template_id=RESERVED):
    return tpl.create_template(
        io.BytesIO(data), "lettre.docx", len(data),
        {"name": "Lettre", "category": "correspondance", "kind": "gabarit"},
        UID, template_id=template_id,
    )


def test_a_reserved_id_is_the_template_s_id(store):
    """Lot 2A (T9): the upload ticket reserves the id at begin_upload."""
    db, bucket = store
    doc, errors = _create_reserved()
    assert errors == [] and doc["id"] == RESERVED
    stored = db.peek(f"doc_templates/{RESERVED}")
    assert stored["storage_path"] == f"users/{UID}/templates/{RESERVED}/v1/lettre.docx"
    assert bucket.objects[stored["storage_path"]].data == V1


def test_a_retried_creation_under_a_reserved_id_lands_on_one_template(store):
    """A finalization retried after a crash (a stale claim reclaimed) must
    not create a second template: the one stored under the reserved id with
    the SAME file is the answer, and its commit is noted."""
    from models import provenance

    db, bucket = store
    first, _ = _create_reserved()
    objects = dict(bucket.objects)
    with provenance.writing_via("mcp", tool="finalize_upload"):
        again, errors = _create_reserved()
        noted = provenance.committed_writes()
    assert errors == [] and again["id"] == first["id"]
    assert len(db.peek_collection("doc_templates")) == 1
    assert bucket.objects == objects                  # nothing uploaded again
    assert noted == (("doc_templates", RESERVED),)


def test_a_reserved_id_holding_another_file_refuses(store):
    db, _ = store
    _create_reserved(V1)
    doc, errors = _create_reserved(V2)
    assert doc is None and errors == [tpl.TEMPLATE_ID_CONFLICT_ERROR]
    assert db.peek(f"doc_templates/{RESERVED}")["sha256"] == _sha(V1)


@pytest.mark.parametrize("bad", ["", "not-a-uuid", RESERVED.upper(),
                                 "7c1d2e3f-4a5b-1c6d-8e7f-9a0b1c2d3e4f",
                                 f"{RESERVED}/versions/1"])
def test_a_reserved_id_that_is_not_a_canonical_uuid4_refuses(store, bad):
    db, bucket = store
    doc, errors = _create_reserved(template_id=bad)
    assert doc is None and errors == [tpl.INVALID_TEMPLATE_ID_ERROR]
    assert db.peek_collection("doc_templates") == {} and bucket.objects == {}


def test_an_unreadable_reserved_id_refuses_before_any_write(store, monkeypatch):
    """« Unreadable » is never « free »: a read failure answers READ_ERROR,
    and the creation stops before its upload."""
    db, bucket = store

    class _Unreadable:
        id = RESERVED

        def get(self):
            raise RuntimeError("store down")

    assert tpl._reserved_answer(_Unreadable(), _sha(V1)) == (None, [tpl.READ_ERROR])
    real = tpl._reserved_answer
    monkeypatch.setattr(tpl, "_reserved_answer",
                        lambda ref, digest: real(_Unreadable(), digest))
    doc, errors = _create_reserved()
    assert doc is None and errors == [tpl.READ_ERROR]
    assert bucket.objects == {} and db.peek_collection("doc_templates") == {}


def test_a_concurrent_creation_that_committed_first_is_the_answer(store):
    """The v1 object is taken by a twin that committed between this call's
    pre-check and its upload: the create-only upload refuses, and the
    twin's template — the same file — is the answer, never an error."""
    db, bucket = store
    original = tpl._store_version_bytes

    def twin_first(template_id, storage_path, data):
        tpl._store_version_bytes = original
        first, errors = _create_reserved()
        assert errors == []
        return original(template_id, storage_path, data)

    tpl._store_version_bytes = twin_first
    try:
        doc, errors = _create_reserved()
    finally:
        tpl._store_version_bytes = original
    assert errors == [] and doc["id"] == RESERVED
    assert len(db.peek_collection("doc_templates")) == 1


def test_create_ignores_every_key_outside_the_whitelist(store):
    """The old merge persisted whatever the caller sent."""
    db, bucket = store
    forged = {
        "name": "Lettre", "category": "autre", "kind": "note",
        "storage_path": "users/x/ailleurs.docx", "id": "forgé",
        "version": 42, "placeholders": ["faux"], "auto_fields": ["faux"],
        "sha256": "0" * 64, "active_for": "note",
        "active_designated_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "active_designated_by": "quelqu'un", "etag": "forgé",
    }
    doc, errors = tpl.create_template(
        io.BytesIO(V1), "lettre.docx", len(V1), forged, UID)
    assert errors == []
    stored = db.peek(f"doc_templates/{doc['id']}")
    assert stored["id"] == doc["id"] != "forgé"
    assert stored["version"] == 1
    assert stored["placeholders"] == ["tribunal"]
    assert stored["auto_fields"] == ["tribunal"]
    assert stored["sha256"] == _sha(V1)
    assert stored["storage_path"].startswith(f"users/{UID}/templates/{doc['id']}/v1/")
    assert stored["etag"] != "forgé"
    assert not set(tpl.ACTIVE_FIELDS) & set(stored)
    assert "users/x/ailleurs.docx" not in bucket.objects


@pytest.mark.parametrize("data, needle", [
    ({"name": "Lettre <client>"}, "chevrons"),
    ({"name": "x" * 121}, "120 caractères"),
    ({"name": "Lettre", "description": "d" * 2001}, "2000 caractères"),
    ({"name": "Lettre", "description": "a <b>gras</b>"}, "chevrons"),
])
def test_create_refuses_what_sanitize_would_alter(store, data, needle):
    db, bucket = store
    got, errors = tpl.create_template(
        io.BytesIO(V1), "lettre.docx", len(V1),
        {"category": "autre", **data}, UID)
    assert got is None and any(needle in e for e in errors), errors
    assert db.peek_collection("doc_templates") == {}
    assert bucket.objects == {}


# ══════════════════════════════════════════════════════════════════════
# 2. Le remplacement : un NOUVEL objet, l'ancien reste, une entrée de plus
# ══════════════════════════════════════════════════════════════════════


def test_a_replacement_keeps_the_old_object_and_adds_a_version(store):
    """THE regression: the old code deleted (or overwrote) version 1."""
    db, bucket = store
    tid = _create()["id"]
    v1_path = f"users/{UID}/templates/{tid}/v1/lettre.docx"
    etag = db.peek(f"doc_templates/{tid}")["etag"]

    # The SAME filename — the case the old code overwrote in place.
    got, errors, changed = _replace(tid, V2, expected_etag=etag)

    assert errors == [] and changed is True
    v2_path = f"users/{UID}/templates/{tid}/v2/lettre.docx"
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["version"] == 2 and stored["storage_path"] == v2_path
    assert stored["sha256"] == _sha(V2)
    assert stored["placeholders"] == ["tribunal", "objet_lettre"]
    assert stored["etag"] != etag and stored["etag"] == got["etag"]
    assert bucket.objects[v1_path].data == V1          # kept, untouched
    assert bucket.objects[v2_path].data == V2
    versions = _versions(db, tid)
    assert sorted(versions) == ["1", "2"]
    assert versions["1"]["storage_path"] == v1_path
    assert versions["1"]["sha256"] == _sha(V1)
    assert versions["2"]["storage_path"] == v2_path
    assert versions["2"]["placeholders"] == ["tribunal", "objet_lettre"]
    assert versions["2"]["restored_from"] is None


def test_the_version_entry_and_the_template_move_in_one_commit(store):
    db, _ = store
    tid = _create()["id"]
    db.reset_logs()
    _replace(tid, V2)
    written = [ops for c in db.commits for ops in c.ops]
    assert (("create", f"doc_templates/{tid}/versions/2") in written)
    assert (("update", f"doc_templates/{tid}") in written)
    # ONE commit carries both — never the template without its entry.
    both = [c for c in db.commits
            if ("create", f"doc_templates/{tid}/versions/2") in c.ops
            and ("update", f"doc_templates/{tid}") in c.ops]
    assert len(both) == 1 and both[0].transaction is not None


def test_a_legacy_template_backfills_the_entry_of_the_file_it_replaces(store):
    """Created before T3: a legacy path, no versions/ entry. Its first
    replacement records the file it had, so that version stays listed and
    restorable — and keeps the object."""
    db, bucket = store
    legacy_path = f"users/{UID}/templates/t1/ancien.docx"
    bucket.put(legacy_path, V1)
    created = datetime(2026, 1, 5, tzinfo=timezone.utc)
    db.seed("doc_templates/t1", {
        **tpl._default_doc(), "id": "t1", "name": "Ancien",
        "category": "autre", "kind": "note_honoraires",
        "filename": "ancien.docx", "original_filename": "Ancien.docx",
        "file_size": len(V1), "storage_path": legacy_path, "version": 3,
        "placeholders": ["tribunal"], "created_at": created,
        "updated_at": created, "etag": "e-legacy",
    })
    assert db.peek("doc_templates/t1")["sha256"] == ""   # never hashed

    got, errors, changed = _replace("t1", V2, filename="nouveau.docx")

    assert errors == [] and changed is True
    assert got["version"] == 4
    assert bucket.objects[legacy_path].data == V1
    versions = _versions(db, "t1")
    assert sorted(versions) == ["3", "4"]
    assert versions["3"]["backfilled"] is True
    assert versions["3"]["storage_path"] == legacy_path
    assert versions["3"]["original_filename"] == "Ancien.docx"
    assert versions["3"]["created_at"] is None      # a v3's install date is unknown
    assert versions["4"]["storage_path"] == f"users/{UID}/templates/t1/v4/nouveau.docx"


def test_the_same_bytes_are_a_no_op(store):
    db, bucket = store
    tid = _create()["id"]
    before = db.peek(f"doc_templates/{tid}")
    objects = dict(bucket.objects)
    db.reset_logs()

    got, errors, changed = _replace(tid, V1, filename="autre-nom.docx")

    assert errors == [] and changed is False
    assert db.peek(f"doc_templates/{tid}") == before
    assert bucket.objects == objects
    assert [c for c in db.commits if c.ops] == []


def test_an_update_with_only_forged_keys_writes_nothing(store):
    """The whitelist on update: storage_path, placeholders, version, id, the
    designation — none of them is ever written from caller data."""
    db, bucket = store
    tid = _create()["id"]
    before = db.peek(f"doc_templates/{tid}")
    got, errors, changed = tpl.update_template(tid, {
        "storage_path": "users/x/y.docx", "placeholders": ["faux"],
        "version": 9, "id": "autre", "active_for": "note",
        "active_designated_by": "x", "sha256": "0" * 64,
        "passthrough_fields": ["faux"], "etag": "forgé",
    })
    assert errors == [] and changed is False
    assert db.peek(f"doc_templates/{tid}") == before


def test_a_metadata_edit_is_a_partial_update_of_what_changed(store):
    db, _ = store
    tid = _create()["id"]
    db.reset_logs()
    got, errors, changed = tpl.update_template(
        tid, {"name": "Lettre (révisée)", "category": "correspondance"})
    assert errors == [] and changed is True
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["name"] == "Lettre (révisée)" and stored["version"] == 1
    assert stored["updated_via"] == "script"
    ops = [op for c in db.commits for op in c.ops]
    assert ops == [("update", f"doc_templates/{tid}")]


def test_a_metadata_edit_never_erases_a_designation_made_meanwhile(
    store, monkeypatch
):
    """The old full-document set() of the PRE-read copy would have erased
    a designation landing between the read and the write."""
    db, _ = store
    tid = _create(kind="note")["id"]
    real = tpl._read_template_strict

    def racing(template_id):
        doc = real(template_id)
        rival, errors = tpl.set_active_template(
            template_id, par="juriste", expected_etag=None)
        assert errors == [], errors
        return doc

    monkeypatch.setattr(tpl, "_read_template_strict", racing)
    got, errors, changed = tpl.update_template(tid, {"description": "Nouveau"})
    assert errors == [] and changed is True
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["description"] == "Nouveau"
    assert stored["active_for"] == "note"


@pytest.mark.parametrize("data, needle", [
    ({"name": "Lettre <client>"}, "chevrons"),
    ({"description": "d" * 2001}, "2000 caractères"),
    ({"name": "   "}, "requis"),
    ({"category": "inventée"}, "Catégorie"),
    ({"kind": "inventé"}, "Type"),
])
def test_update_refuses_what_it_used_to_mangle(store, data, needle):
    db, _ = store
    tid = _create()["id"]
    before = db.peek(f"doc_templates/{tid}")
    got, errors, changed = tpl.update_template(tid, data)
    assert got is None and changed is False
    assert any(needle in e for e in errors), errors
    assert db.peek(f"doc_templates/{tid}") == before


# ══════════════════════════════════════════════════════════════════════
# 3. La concurrence : la version qu'on a lue, ou rien
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_etag_refuses_and_removes_only_its_own_object(store):
    db, bucket = store
    tid = _create()["id"]
    path = f"doc_templates/{tid}"
    stored = db.peek(path)
    stored["description"] = "Écrit ailleurs"
    stored["etag"] = "rival-etag"
    db.external_write(path, stored)
    before = db.peek(path)

    got, errors, changed = _replace(tid, V2, expected_etag="e-page")

    assert got is None and errors == [concurrency.STALE_ETAG_ERROR]
    assert db.peek(path) == before
    assert sorted(_versions(db, tid)) == ["1"]
    assert list(bucket.objects) == [f"users/{UID}/templates/{tid}/v1/lettre.docx"]


def test_a_stale_expected_version_refuses(store):
    db, bucket = store
    tid = _create()["id"]
    got, errors, changed = _replace(tid, V2, expected_version=7)
    assert errors == [concurrency.STALE_ETAG_ERROR]
    assert db.peek(f"doc_templates/{tid}")["version"] == 1
    assert len(bucket.objects) == 1


def test_a_version_installed_meanwhile_refuses_the_replacement_it_would_replace(
    store, monkeypatch
):
    """No etag at all: a replacement is still built on the version it READ.
    Another replacement committed in between → refused, the rival's bytes
    and entry intact, this call's own object removed."""
    db, bucket = store
    tid = _create()["id"]
    real = tpl._read_template_strict
    fired = []

    def racing(template_id):
        doc = real(template_id)
        if not fired:
            fired.append(1)
            _, errs, _ = _replace(template_id, V3, filename="rival.docx")
            assert errs == []
        return doc

    monkeypatch.setattr(tpl, "_read_template_strict", racing)
    got, errors, changed = _replace(tid, V2, filename="moi.docx")

    assert errors == [concurrency.STALE_ETAG_ERROR]
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["version"] == 2 and stored["sha256"] == _sha(V3)
    assert bucket.objects[f"users/{UID}/templates/{tid}/v2/rival.docx"].data == V3
    assert f"users/{UID}/templates/{tid}/v2/moi.docx" not in bucket.objects
    assert sorted(_versions(db, tid)) == ["1", "2"]
    assert _versions(db, tid)["2"]["sha256"] == _sha(V3)


def test_two_replacements_can_never_land_on_one_object(store):
    """Create-only upload: an object already at the version path (a
    replacement in flight, same filename) is never overwritten."""
    db, bucket = store
    tid = _create()["id"]
    in_flight = f"users/{UID}/templates/{tid}/v2/lettre.docx"
    bucket.put(in_flight, b"en cours")        # young: a live replacement

    got, errors, changed = _replace(tid, V2)

    assert errors == [tpl.VERSION_IN_PROGRESS_ERROR]
    assert bucket.objects[in_flight].data == b"en cours"
    assert db.peek(f"doc_templates/{tid}")["version"] == 1


def test_an_old_unreferenced_orphan_is_cleared_then_the_replacement_lands(store):
    """A failed replacement whose rollback failed too left its object: past
    the gunicorn bound it belongs to no live request."""
    db, bucket = store
    tid = _create()["id"]
    orphan = f"users/{UID}/templates/{tid}/v2/lettre.docx"
    bucket.put(orphan, b"orphelin",
               time_created=datetime.now(timezone.utc) - timedelta(hours=1))

    got, errors, changed = _replace(tid, V2)

    assert errors == [] and changed is True
    assert bucket.objects[orphan].data == V2
    assert db.peek(f"doc_templates/{tid}")["storage_path"] == orphan


def test_a_failed_commit_removes_this_calls_object_and_nothing_else(store):
    db, bucket = store
    tid = _create()["id"]

    def boom(info):
        if any(p == f"doc_templates/{tid}" for _op, p in info.ops):
            raise RuntimeError("commit refusé")

    remove = db.add_commit_hook(boom)
    got, errors, changed = _replace(tid, V2)
    remove()

    assert got is None and errors == [tpl.SAVE_ERROR]
    assert list(bucket.objects) == [f"users/{UID}/templates/{tid}/v1/lettre.docx"]
    assert db.peek(f"doc_templates/{tid}")["version"] == 1
    assert sorted(_versions(db, tid)) == ["1"]


def test_a_commit_that_landed_while_its_answer_was_lost_keeps_its_object(
    store, monkeypatch
):
    """The rollback re-reads before deleting: when the record already names
    the object (the commit landed, its answer was lost), the object is the
    version's bytes — deleting it would leave a template without its file."""
    db, bucket = store
    tid = _create()["id"]
    real_transaction = db.transaction

    def lossy():
        txn = real_transaction()
        real_commit = txn._commit

        def _commit():
            real_commit()
            raise RuntimeError("réponse perdue")

        txn._commit = _commit
        return txn

    monkeypatch.setattr(db, "transaction", lossy)
    got, errors, changed = _replace(tid, V2)

    assert got is None and errors == [tpl.SAVE_ERROR]
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["version"] == 2                    # it did land
    assert bucket.objects[stored["storage_path"]].data == V2   # and kept


# ══════════════════════════════════════════════════════════════════════
# 4. « Rétablir » : une version antérieure devient la version N+1
# ══════════════════════════════════════════════════════════════════════


def test_restore_creates_version_n_plus_1_and_rewrites_no_history(store):
    db, bucket = store
    tid = _create()["id"]
    _replace(tid, V2)
    history_before = _versions(db, tid)
    etag = db.peek(f"doc_templates/{tid}")["etag"]

    got, errors, changed = tpl.restore_template_version(
        tid, 1, par="juriste@example.com", expected_etag=etag)

    assert errors == [] and changed is True
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["version"] == 3 and stored["sha256"] == _sha(V1)
    v3 = f"users/{UID}/templates/{tid}/v3/lettre.docx"
    assert stored["storage_path"] == v3 and bucket.objects[v3].data == V1
    versions = _versions(db, tid)
    assert sorted(versions) == ["1", "2", "3"]
    assert versions["3"]["restored_from"] == 1
    assert versions["3"]["restored_by"] == "juriste@example.com"
    # Append-only: versions 1 and 2 are exactly as they were.
    assert versions["1"] == history_before["1"]
    assert versions["2"] == history_before["2"]
    # Every object is still there.
    assert bucket.objects[versions["1"]["storage_path"]].data == V1
    assert bucket.objects[versions["2"]["storage_path"]].data == V2


def test_restore_of_the_current_version_is_refused(store):
    db, _ = store
    tid = _create()["id"]
    got, errors, changed = tpl.restore_template_version(
        tid, 1, par="j", expected_etag=None)
    assert errors == [tpl.VERSION_IS_CURRENT_ERROR]


def test_restore_with_a_stale_etag_writes_nothing(store):
    db, bucket = store
    tid = _create()["id"]
    _replace(tid, V2)
    before = db.peek(f"doc_templates/{tid}")
    objects = dict(bucket.objects)
    got, errors, changed = tpl.restore_template_version(
        tid, 1, par="j", expected_etag="e-périmé")
    assert errors == [concurrency.STALE_ETAG_ERROR]
    assert db.peek(f"doc_templates/{tid}") == before
    assert bucket.objects == objects


def test_restore_refuses_bytes_that_no_longer_match_their_recorded_hash(store):
    db, bucket = store
    tid = _create()["id"]
    _replace(tid, V2)
    v1_path = _versions(db, tid)["1"]["storage_path"]
    bucket.put(v1_path, V3)                       # altered behind our back
    got, errors, changed = tpl.restore_template_version(
        tid, 1, par="j", expected_etag=None)
    assert errors == [tpl.VERSION_INTEGRITY_ERROR]
    assert db.peek(f"doc_templates/{tid}")["version"] == 2


@pytest.mark.parametrize("version", [7, "x"])
def test_restore_of_an_unknown_version_is_refused(store, version):
    tid = _create()["id"]
    _replace(tid, V2)
    got, errors, changed = tpl.restore_template_version(
        tid, version, par="j", expected_etag=None)
    assert errors == [tpl.VERSION_NOT_FOUND_ERROR]


def test_restore_of_a_version_whose_file_vanished_is_refused(store):
    db, bucket = store
    tid = _create()["id"]
    _replace(tid, V2)
    bucket.remove(_versions(db, tid)["1"]["storage_path"])
    got, errors, changed = tpl.restore_template_version(
        tid, 1, par="j", expected_etag=None)
    assert errors == [tpl.VERSION_FILE_MISSING_ERROR]


def test_restoring_bytes_identical_to_the_current_file_is_a_no_op(store):
    db, _ = store
    tid = _create()["id"]
    _replace(tid, V2)
    _replace(tid, V1)                               # v3 holds V1 again
    before = db.peek(f"doc_templates/{tid}")
    got, errors, changed = tpl.restore_template_version(
        tid, 1, par="j", expected_etag=None)
    assert errors == [] and changed is False
    assert db.peek(f"doc_templates/{tid}") == before


def test_list_versions_is_newest_first(store):
    tid = _create()["id"]
    _replace(tid, V2)
    _replace(tid, V3)
    assert [v["version"] for v in tpl.list_versions(tid)] == [3, 2, 1]


# ══════════════════════════════════════════════════════════════════════
# 5. La suppression emporte TOUTES les versions
# ══════════════════════════════════════════════════════════════════════


def test_delete_removes_every_version_object_and_entry(store):
    db, bucket = store
    tid = _create()["id"]
    _replace(tid, V2)
    _replace(tid, V3)
    assert len(bucket.objects) == 3

    ok, message = tpl.delete_template(tid)

    assert ok is True and message == ""
    assert db.peek(f"doc_templates/{tid}") is None
    assert _versions(db, tid) == {}
    assert bucket.objects == {}


def test_delete_of_an_unknown_template_is_refused(store):
    ok, message = tpl.delete_template("inconnu")
    assert ok is False and message == tpl.NOT_FOUND_ERROR


def test_a_failed_object_delete_still_deletes_the_records(store, monkeypatch):
    """Records first: an orphan object costs storage, a template without
    its file costs a document."""
    db, bucket = store
    tid = _create()["id"]
    logged = []
    monkeypatch.setattr(tpl, "log_unexpected",
                        lambda msg, **kw: logged.append(msg))

    class _Failing:
        def blob(self, name):
            blob = bucket.blob(name)
            blob.delete = lambda **kw: (_ for _ in ()).throw(RuntimeError("503"))
            return blob

    monkeypatch.setattr(tpl.storage, "bucket", lambda: _Failing())
    ok, _ = tpl.delete_template(tid)
    assert ok is True
    assert db.peek(f"doc_templates/{tid}") is None
    assert logged == ["template file delete failed"]


# ══════════════════════════════════════════════════════════════════════
# 6. Le type du gabarit ACTIF ne change pas sous lui
# ══════════════════════════════════════════════════════════════════════


def test_the_kind_of_the_designated_template_cannot_change(store):
    db, _ = store
    tid = _create(kind="note_honoraires")["id"]
    tpl.set_active_template(tid, par="j", expected_etag=None)
    before = db.peek(f"doc_templates/{tid}")
    got, errors, changed = tpl.update_template(tid, {"kind": "gabarit"})
    assert got is None
    assert errors == [tpl.ACTIVE_KIND_CHANGE_ERROR.format(kind="Note d'honoraires")]
    assert db.peek(f"doc_templates/{tid}") == before


def test_a_template_that_is_not_designated_may_change_kind(store):
    db, _ = store
    tid = _create(kind="note_honoraires")["id"]
    got, errors, changed = tpl.update_template(tid, {"kind": "gabarit"})
    assert errors == [] and db.peek(f"doc_templates/{tid}")["kind"] == "gabarit"


# ══════════════════════════════════════════════════════════════════════
# 7. Balayages du source
# ══════════════════════════════════════════════════════════════════════


def _source() -> str:
    return (ATHENA / "models" / "doc_template.py").read_text(encoding="utf-8")


def test_a_version_entry_is_only_ever_created_never_set_or_updated():
    """Write-once by construction: every write that names the versions
    subcollection is a create(); no set/update/delete touches one outside
    delete_template (which removes the whole template)."""
    tree = ast.parse(_source())
    creates, others = [], []
    top_level = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    for fn in top_level:                     # nested bodies walked with them
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("create", "set", "update", "delete")
                    and node.args):
                continue
            target = ast.unparse(node.args[0])
            if "versions" in target or "_versions_ref" in target:
                (creates if node.func.attr == "create" else others).append(
                    (fn.name, node.func.attr, target))
    assert creates, "the sweep found no version write at all"
    # The two writers of an entry: the creation (v1) and the one commit
    # path of a new version (replacement and restore alike).
    assert {c[0] for c in creates} == {"create_template", "_commit_update"}
    assert others == [], others
    # The only other statement that touches an entry deletes the WHOLE
    # template with it, inside one transaction.
    deletes = [fn.name for fn in top_level for node in ast.walk(fn)
               if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute)
               and node.func.attr == "delete" and node.args
               and ast.unparse(node.args[0]) == "entry.reference"]
    assert deletes == ["delete_template"]


def test_no_template_path_is_built_without_a_version_segment():
    """One builder, and it always carries v{N}: a version object can never
    share a path with another version — nor with a legacy object."""
    tree = ast.parse(_source())
    builders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if (isinstance(node, ast.JoinedStr) and node.values
                    and isinstance(node.values[0], ast.Constant)
                    and str(node.values[0].value).startswith("users/")):
                builders.append((fn.name, ast.unparse(node)))
    assert [b[0] for b in builders] == ["_template_object_path"]
    assert "/v{" in builders[0][1]


def test_no_template_upload_can_overwrite():
    """Every template upload is create-only (if_generation_match=0)."""
    tree = ast.parse(_source())
    uploads = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr.startswith("upload_from")]
    assert uploads
    for call in uploads:
        kw = {k.arg: k.value for k in call.keywords}
        assert "if_generation_match" in kw
        assert isinstance(kw["if_generation_match"], ast.Constant)
        assert kw["if_generation_match"].value == 0


# ── The silent failure branches now leave a trace (2026-09-30) ────────────


def _unexpected(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "pallas.unexpected"]


def test_an_unreadable_reference_check_answers_none_and_says_so(
        monkeypatch, caplog):
    import logging

    class _Down:
        def collection(self, _name):
            return self

        def document(self, _id):
            return self

        def get(self, *_a, **_k):
            raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(tpl, "db", _Down())
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        assert tpl._referenced("t1", "users/u/templates/t1/v2/a.docx") is None
    assert _unexpected(caplog) == ["template reference check unreadable"]


class _OrphanBlob:
    generation = 7

    def __init__(self, *, reload_error=None, delete_error=None):
        self._reload_error, self._delete_error = reload_error, delete_error
        self.time_created = datetime.now(timezone.utc) - timedelta(days=2)

    def reload(self):
        if self._reload_error:
            raise self._reload_error

    def delete(self, **_kwargs):
        if self._delete_error:
            raise self._delete_error


def _orphan_bucket(monkeypatch, blob):
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(tpl, "_referenced", lambda *_a: False)


def test_a_failed_orphan_check_refuses_and_says_so(monkeypatch, caplog):
    import logging

    _orphan_bucket(monkeypatch, _OrphanBlob(reload_error=RuntimeError("gcs")))
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        assert tpl._clear_stale_orphan("t1", "users/u/t1/v2/a.docx") is False
    assert _unexpected(caplog) == ["template orphan check failed"]


def test_a_failed_orphan_delete_refuses_and_says_so(monkeypatch, caplog):
    import logging

    _orphan_bucket(monkeypatch, _OrphanBlob(delete_error=RuntimeError("gcs")))
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        assert tpl._clear_stale_orphan("t1", "users/u/t1/v2/a.docx") is False
    assert _unexpected(caplog) == ["template orphan delete failed"]


def test_an_old_unreferenced_orphan_is_cleared_quietly(monkeypatch, caplog):
    import logging

    _orphan_bucket(monkeypatch, _OrphanBlob())
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        assert tpl._clear_stale_orphan("t1", "users/u/t1/v2/a.docx") is True
    assert _unexpected(caplog) == []
