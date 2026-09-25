"""La concurrence optimiste — ``models/concurrency.py`` (plan lot 0a, règle 3).

Une modification ne s'engage que contre la version que son appelant a lue.
Ce que ces tests épinglent, sur le VRAI client Firestore au-dessus du faux
serveur partagé (``tests/_fake_firestore.py``), en relisant ce qui est
STOCKÉ — jamais un dictionnaire remis à un mock :

1. Le chemin d'origine (``expected_etag=None``) est octet pour octet celui
   d'avant : UN ``set()`` (ou ``update()``) hors transaction, aucune lecture.
2. Le chemin gardé relit DANS une transaction, AVANT toute écriture, et
   n'engage que si l'etag vivant est celui qu'on attend.
3. Périmé, disparu, ou perdu à la course : rien n'est écrit — ni le
   document, ni ce qui voyage avec lui — et la reprise du décorateur réel
   ``firestore.transactional`` ne se change jamais en écrasement aveugle.
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import concurrency

from tests._fake_firestore import FakeFirestore  # noqa: E402

PATH = "parties/p1"


@pytest.fixture
def db():
    fake = FakeFirestore()
    fake.seed(PATH, {"id": "p1", "last_name": "Tremblay", "notes": "",
                     "etag": "e0"})
    fake.reset_logs()
    return fake


def _ref(db, path=PATH):
    coll, _, doc_id = path.rpartition("/")
    return db.collection(coll).document(doc_id)


def _new(db, **over):
    doc = db.peek(PATH)
    doc.update(over)
    return doc


# ══════════════════════════════════════════════════════════════════════
# 1. Le chemin d'origine est inchangé
# ══════════════════════════════════════════════════════════════════════


def test_legacy_path_is_one_plain_set_with_no_read_and_no_transaction(db):
    concurrency.commit_document(
        _ref(db), _new(db, notes="x", etag="e1"), expected_etag=None)
    assert db.reads == []
    assert [(c.transaction, c.ops) for c in db.commits] == [
        (None, (("set", PATH),))]
    assert db.peek(PATH)["notes"] == "x"


def test_legacy_path_ignores_a_stale_read_etag_exactly_as_before(db):
    """None asserts nothing: the read etag is not consulted, the write
    lands — last-write-wins, which is today's behaviour on that path."""
    db.external_write(PATH, _new(db, etag="e9"))
    concurrency.commit_document(
        _ref(db), _new(db, notes="x", etag="e1"),
        expected_etag=None, read_etag="e0")
    assert db.peek(PATH)["notes"] == "x"


def test_legacy_extra_sets_are_written_first_then_the_document(db):
    """« Le journal AVANT le cache » — the order the models used before."""
    journal = _ref(db, "parties/p1/analyses/a1")
    concurrency.commit_document(
        _ref(db), _new(db, etag="e1"), expected_etag=None,
        extra_sets=((journal, {"k": 1}),))
    assert [c.ops for c in db.commits] == [
        (("set", "parties/p1/analyses/a1"),), (("set", PATH),)]


def test_legacy_fields_path_is_one_plain_update(db):
    concurrency.commit_fields(_ref(db), {"notes": "y", "etag": "e1"},
                              expected_etag=None)
    assert db.reads == []
    assert [(c.transaction, c.ops) for c in db.commits] == [
        (None, (("update", PATH),))]


# ══════════════════════════════════════════════════════════════════════
# 2. Le chemin gardé : lecture transactionnelle, puis écriture
# ══════════════════════════════════════════════════════════════════════


def test_a_matching_etag_commits_in_one_transaction_after_a_transactional_read(db):
    concurrency.commit_document(
        _ref(db), _new(db, notes="x", etag="e1"),
        expected_etag="e0", read_etag="e0")
    assert db.peek(PATH)["notes"] == "x"
    assert db.peek(PATH)["etag"] == "e1"
    # The check read went THROUGH the transaction (it is what the commit's
    # serializability is judged against), and nothing was read outside it.
    assert [(r.paths, r.transactional) for r in db.reads] == [((PATH,), True)]
    assert len(db.commits) == 1
    assert db.commits[0].transaction is not None
    assert db.commits[0].ops == (("set", PATH),)


def test_extra_sets_commit_atomically_with_the_document(db):
    journal = _ref(db, "parties/p1/analyses/a1")
    concurrency.commit_document(
        _ref(db), _new(db, etag="e1"), expected_etag="e0",
        extra_sets=((journal, {"k": 1}),))
    assert len(db.commits) == 1
    assert set(db.commits[0].ops) == {
        ("set", PATH), ("set", "parties/p1/analyses/a1")}
    assert db.peek("parties/p1/analyses/a1") == {"k": 1}


def test_guarded_fields_update_stages_exactly_the_fields(db):
    before = db.peek(PATH)
    concurrency.commit_fields(_ref(db), {"notes": "y", "etag": "e1"},
                              expected_etag="e0", read_etag="e0")
    after = db.peek(PATH)
    assert db.commits[0].ops == (("update", PATH),)
    assert db.commits[0].transaction is not None
    assert after == {**before, "notes": "y", "etag": "e1"}


def test_an_empty_expected_etag_matches_a_legacy_document_without_one(db):
    """The connector hands out '' for a pre-Rule-7 row; writing it back
    must work, or such a row could never be edited under the guard."""
    db.seed("parties/old", {"id": "old", "notes": ""})
    concurrency.commit_document(
        _ref(db, "parties/old"), {"id": "old", "notes": "z", "etag": "e1"},
        expected_etag="", read_etag="")
    assert db.peek("parties/old")["notes"] == "z"


# ══════════════════════════════════════════════════════════════════════
# 3. Périmé, disparu, perdu à la course : rien n'est écrit
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_expected_etag_writes_nothing(db):
    journal = _ref(db, "parties/p1/analyses/a1")
    before = db.peek(PATH)
    with pytest.raises(concurrency.StaleWrite):
        concurrency.commit_document(
            _ref(db), _new(db, notes="x", etag="e1"), expected_etag="old",
            extra_sets=((journal, {"k": 1}),))
    assert db.peek(PATH) == before
    assert db.peek("parties/p1/analyses/a1") is None
    assert db.commits == []


def test_a_stale_read_etag_writes_nothing_even_when_expected_matches(db):
    """The document to write was built from an OLDER read than the live
    version: committing it would resurrect what changed in between."""
    with pytest.raises(concurrency.StaleWrite):
        concurrency.commit_document(
            _ref(db), _new(db, notes="x", etag="e1"),
            expected_etag="e0", read_etag="e-older")
    assert db.peek(PATH)["notes"] == ""
    assert db.commits == []


def test_a_stale_fields_update_writes_nothing(db):
    with pytest.raises(concurrency.StaleWrite):
        concurrency.commit_fields(_ref(db), {"notes": "y", "etag": "e1"},
                                  expected_etag="old")
    assert db.peek(PATH)["notes"] == "" and db.commits == []


def test_a_vanished_document_is_never_resurrected(db):
    """A guarded set() on a document deleted since the read would CREATE
    it again — with a full copy of the deleted content."""
    db.external_delete(PATH)
    with pytest.raises(concurrency.Vanished):
        concurrency.commit_document(
            _ref(db), {"id": "p1", "etag": "e1"}, expected_etag="e0")
    assert db.peek(PATH) is None and db.commits == []
    with pytest.raises(concurrency.Vanished):
        concurrency.commit_fields(_ref(db), {"etag": "e1"},
                                  expected_etag="e0")
    assert db.peek(PATH) is None


def test_a_writer_racing_the_commit_aborts_it_and_the_retry_refuses(db):
    """The concurrent write lands AFTER our transactional read, before our
    commit. The server aborts the commit; the real decorator re-runs the
    body, whose re-read now sees the new etag — StaleWrite, never a blind
    overwrite on the second attempt."""
    raced = []

    def hook(info):
        if info.transaction is not None and not raced:
            raced.append(info.index)
            db.external_write(PATH, {**db.peek(PATH), "notes": "rival",
                                     "etag": "e-rival"})

    db.add_commit_hook(hook)
    with pytest.raises(concurrency.StaleWrite):
        concurrency.commit_document(
            _ref(db), _new(db, notes="mine", etag="e1"),
            expected_etag="e0", read_etag="e0")
    stored = db.peek(PATH)
    assert stored["notes"] == "rival" and stored["etag"] == "e-rival"
    assert raced == [0]
    assert db.commits == []  # the rival wrote directly; we never committed
    # Two transactional reads: the first attempt, then the retry's re-read.
    assert [r.transactional for r in db.reads] == [True, True]


def test_a_store_failure_propagates_as_is_and_writes_nothing(db):
    def hook(info):
        raise RuntimeError("commit refused by the server")

    db.add_commit_hook(hook)
    with pytest.raises(RuntimeError):
        concurrency.commit_document(
            _ref(db), _new(db, notes="x", etag="e1"), expected_etag="e0")
    assert db.peek(PATH)["notes"] == ""


# ══════════════════════════════════════════════════════════════════════
# 4. Les petits prédicats
# ══════════════════════════════════════════════════════════════════════


def test_the_predicates():
    assert concurrency.etag_of({"etag": "e0"}) == "e0"
    assert concurrency.etag_of({}) == concurrency.etag_of(None) == ""
    assert concurrency.matches({"etag": "e0"}, None)
    assert concurrency.matches({"etag": "e0"}, "e0")
    assert not concurrency.matches({"etag": "e0"}, "e1")
    assert concurrency.matches({}, "")
    assert concurrency.is_stale([concurrency.STALE_ETAG_ERROR])
    assert concurrency.is_stale(["Autre.", concurrency.STALE_ETAG_ERROR])
    assert not concurrency.is_stale(["Contact introuvable."])
    assert not concurrency.is_stale([]) and not concurrency.is_stale(None)


def test_the_refusal_is_french_and_names_no_content():
    msg = concurrency.STALE_ETAG_ERROR
    assert "modifié entre-temps" in msg and "PAS été enregistrés" in msg
    assert "{" not in msg  # a constant, never a template to fill with data
