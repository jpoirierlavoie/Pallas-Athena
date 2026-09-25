"""Les instantanés de révision (règle 4 du plan) — ``models/revision.py``.

Le module ne sert encore à personne : c'est la primitive du lot 1 (D8). On
épingle donc sa DOCTRINE avant son premier appelant :

1. la forme — l'exception à la règle 7 (``created_at`` seul, ni
   ``updated_at`` ni ``etag``), la provenance tirée du contexte ;
2. l'écriture passe par la transaction de l'APPELANT : le module n'écrit
   rien lui-même, et sur le vrai client la révision et le remplacement
   s'engagent ensemble ou pas du tout ;
3. les refus, AVANT toute mise en file ;
4. les lectures — la liste échoue OUVERT sur un index simple, la lecture
   d'une révision LÈVE ;
5. aucun verbe ne modifie ni n'efface une révision, et aucun autre module
   n'atteint la sous-collection par son nom.

Le magasin est le faux partagé (``tests/_fake_firestore.py``) : le VRAI
client, un serveur en mémoire qui tamponne les écritures transactionnelles.
"""

import ast
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import concurrency, provenance
    from models import revision as rev

from tests._fake_firestore import install  # noqa: E402

ATHENA = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    """Every test runs against the shared fake — a reference built by
    ``build_revision`` is then a REAL ``DocumentReference``."""
    return install(monkeypatch, rev)


def _build(**over):
    kwargs = {
        "parent_collection": "notes",
        "parent_id": "n1",
        "field": "content",
        "previous_value": "Ancien texte",
        "previous_etag": "e-old",
        "new_etag": "e-new",
        "now": NOW,
    }
    kwargs.update(over)
    return rev.build_revision(**kwargs)


# ── 1. La forme ────────────────────────────────────────────────────────────


def test_the_shape_is_the_rule_7_exception():
    ref, data = _build()
    assert set(data) == {
        "id", "parent_collection", "parent_id", "field", "previous_value",
        "previous_length", "previous_etag", "new_etag", "via", "tool",
        "created_at",
    }
    assert "updated_at" not in data and "etag" not in data
    assert data["created_at"] == NOW
    assert data["previous_length"] == len("Ancien texte")
    # Rule 6: a UUIDv4 id, which is also the document id.
    assert ref.id == data["id"] and len(data["id"]) == 36
    assert ref.path == f"notes/n1/{rev.SUBCOLLECTION}/{data['id']}"


def test_two_revisions_never_share_an_id():
    assert _build()[1]["id"] != _build()[1]["id"]


def test_the_writer_comes_from_the_provenance_context():
    _, outside = _build()
    assert outside["via"] == "script" and outside["tool"] == ""
    with provenance.writing_via("mcp", tool="edit_analyse"):
        _, inside = _build()
    assert inside["via"] == "mcp" and inside["tool"] == "edit_analyse"


def test_an_empty_previous_value_and_a_legacy_etag_are_legitimate():
    """A note may have been empty, and a record written before Rule 7 has
    no etag: both are real versions to record."""
    _, data = _build(previous_value="", previous_etag="")
    assert data["previous_value"] == "" and data["previous_etag"] == ""


@pytest.mark.parametrize("field", rev.VALID_FIELDS)
def test_every_declared_field_is_accepted(field):
    assert _build(field=field)[1]["field"] == field


# ── 2. L'écriture passe par la transaction de l'appelant ──────────────────


def test_building_a_revision_writes_nothing(fake):
    _build()
    assert fake.commits == []


def test_a_guarded_commit_writes_the_revision_and_the_replacement_together(fake):
    fake.seed("notes/n1", {"id": "n1", "content": "Ancien texte", "etag": "e-old"})
    ref, data = _build()
    note_ref = fake.collection("notes").document("n1")
    concurrency.commit_document(
        note_ref,
        {"id": "n1", "content": "Nouveau texte", "etag": "e-new"},
        expected_etag="e-old",
        read_etag="e-old",
        extra_sets=[(ref, data)],
    )
    assert fake.peek("notes/n1")["content"] == "Nouveau texte"
    stored = fake.peek(ref.path)
    assert stored["previous_value"] == "Ancien texte"
    assert stored["new_etag"] == fake.peek("notes/n1")["etag"]
    # ONE commit carried both writes — atomic, never two round trips.
    assert len(fake.commits) == 1
    assert {path for _, path in fake.commits[0].ops} == {"notes/n1", ref.path}


def test_a_stale_replacement_leaves_no_revision_behind(fake):
    """The reason this module never writes on its own: a history entry for
    a replacement that was REFUSED would record a change that never
    happened."""
    fake.seed("notes/n1", {"id": "n1", "content": "Autre texte", "etag": "e-live"})
    ref, data = _build()
    with pytest.raises(concurrency.StaleWrite):
        concurrency.commit_document(
            fake.collection("notes").document("n1"),
            {"id": "n1", "content": "Nouveau texte", "etag": "e-new"},
            expected_etag="e-old",
            extra_sets=[(ref, data)],
        )
    assert fake.peek(ref.path) is None
    assert fake.peek("notes/n1")["content"] == "Autre texte"
    assert fake.peek_collection(f"notes/n1/{rev.SUBCOLLECTION}") == {}


# ── 3. Les refus, avant toute mise en file ────────────────────────────────


def test_the_snapshot_cap_is_inclusive():
    at_cap = "x" * rev.MAX_SNAPSHOT_CHARS
    assert _build(previous_value=at_cap)[1]["previous_length"] == len(at_cap)
    with pytest.raises(rev.RevisionRefused):
        _build(previous_value=at_cap + "x")


def test_the_cap_keeps_a_worst_case_snapshot_under_the_document_limit():
    """1 MiB per Firestore document, four bytes per character at worst."""
    assert rev.MAX_SNAPSHOT_CHARS * 4 < 1024 * 1024


@pytest.mark.parametrize("over", [
    {"parent_collection": "dossiers"},        # not a declared parent
    {"parent_collection": "Notes"},
    {"field": "title"},                       # not a declared field
    {"field": "bloc:I"},
    {"parent_id": ""},
    {"parent_id": "a/b"},                     # a path in disguise
    {"parent_id": ".."},
    {"parent_id": "__name__"},
    {"parent_id": None},
    {"previous_value": None},
    {"previous_value": ["liste"]},
    {"previous_etag": None},
    {"new_etag": ""},                         # built before the stamp
    {"now": datetime(2026, 9, 25, 14, 0)},    # naive (Rule 5)
    {"now": "2026-09-25"},
])
def test_a_malformed_revision_is_refused_before_it_is_staged(fake, over):
    with pytest.raises(rev.RevisionRefused):
        _build(**over)
    assert fake.commits == []


def test_the_refusal_is_a_value_error():
    """A caller that already maps ValueError to a French refusal needs no
    new except clause."""
    assert issubclass(rev.RevisionRefused, ValueError)


# ── 4. Les lectures ───────────────────────────────────────────────────────


def _seed_revisions(fake, n=3, parent="n1"):
    ids = []
    for i in range(n):
        ref, data = _build(
            parent_id=parent, previous_value=f"version {i}",
            now=NOW + timedelta(minutes=i),
        )
        fake.seed(ref.path, data)
        ids.append(data["id"])
    return ids


def test_the_listing_is_newest_first_and_leaves_the_text_out(fake):
    ids = _seed_revisions(fake)
    rows = rev.list_revisions("notes", "n1")
    assert [r["id"] for r in rows] == list(reversed(ids))
    assert all("previous_value" not in r for r in rows)
    assert rows[0]["previous_length"] == len("version 2")


def test_the_listing_can_carry_the_text(fake):
    _seed_revisions(fake)
    rows = rev.list_revisions("notes", "n1", include_values=True)
    assert rows[0]["previous_value"] == "version 2"


def test_the_listing_stays_inside_its_parent(fake):
    _seed_revisions(fake, parent="n1")
    _seed_revisions(fake, n=2, parent="n2")
    assert len(rev.list_revisions("notes", "n1")) == 3
    assert len(rev.list_revisions("notes", "n2")) == 2


def test_the_listing_is_bounded(fake):
    _seed_revisions(fake, n=5)
    assert len(rev.list_revisions("notes", "n1", limit=2)) == 2
    assert len(rev.list_revisions("notes", "n1", limit=0)) == 5   # default 20
    assert len(rev.list_revisions("notes", "n1", limit=10_000)) == 5


def test_the_listing_needs_no_composite_index(fake, monkeypatch):
    """One single-field order_by and no filter: the automatic index of the
    subcollection serves it, so nothing has to deploy before this code."""
    seen = []
    server = fake._fake_server
    real = server.run_query

    def spy(request, metadata=None, **kwargs):
        seen.append(request["structured_query"]._pb)
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", spy)
    _seed_revisions(fake)
    rev.list_revisions("notes", "n1")
    assert len(seen) == 1
    sq = seen[0]
    assert [o.field.field_path for o in sq.order_by] == ["created_at"]
    assert not sq.HasField("where")


def test_the_listing_fails_open_and_says_so(fake, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(fake._fake_server, "run_query", boom)
    logged = []
    monkeypatch.setattr(rev, "log_unexpected",
                        lambda msg, **kw: logged.append((msg, kw)))
    assert rev.list_revisions("notes", "n1") == []
    assert logged and logged[0][1] == {"parent_collection": "notes"}


def test_the_listing_of_an_undeclared_parent_is_empty(fake):
    assert rev.list_revisions("dossiers", "d1") == []
    assert rev.list_revisions("notes", "a/b") == []


def test_one_revision_is_read_with_its_text(fake):
    ids = _seed_revisions(fake)
    got = rev.get_revision("notes", "n1", ids[0])
    assert got["previous_value"] == "version 0"
    assert rev.get_revision("notes", "n1", "absent") is None
    # Keyed under its parent: another note's path does not reach it.
    assert rev.get_revision("notes", "n2", ids[0]) is None


def test_reading_one_revision_raises_rather_than_inventing_an_absence(
    fake, monkeypatch
):
    """A restore must work from the truth: a swallowed read error would
    read as « no such revision »."""
    def boom(*_a, **_kw):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(fake._fake_server, "batch_get_documents", boom)
    with pytest.raises(RuntimeError):
        rev.get_revision("notes", "n1", "r1")
    with pytest.raises(rev.RevisionRefused):
        rev.get_revision("notes", "n1", "a/b")


# ── 5. Aucun verbe ne modifie ni n'efface une révision ────────────────────

_WRITE_ATTRS = {
    "set", "update", "delete", "create", "add", "commit", "batch",
    "transaction", "bulk_writer", "recursive_delete",
}


def _module_tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_the_module_defines_no_mutating_verb():
    tree = _module_tree(ATHENA / "models" / "revision.py")
    names = [n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert names, "the sweep found no function at all"
    for name in names:
        assert not name.startswith(("update", "delete", "remove", "purge",
                                    "edit", "replace", "set_")), name


def test_the_module_makes_no_write_call():
    """build_revision RETURNS a pair; the caller stages it. A write call
    here would let a revision land apart from the replacement it records."""
    tree = _module_tree(ATHENA / "models" / "revision.py")
    calls = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "collection" in calls, "the sweep is not reading the module"
    assert not (set(calls) & _WRITE_ATTRS), set(calls) & _WRITE_ATTRS


def _reaches_the_subcollection(node: ast.AST) -> bool:
    """A ``.collection(<revisions>)`` call: the literal name, or this
    module's constant read through its module (``revision.SUBCOLLECTION``).
    A bare ``SUBCOLLECTION`` name is some OTHER module's constant."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "collection" and node.args):
        return False
    arg = node.args[0]
    if isinstance(arg, ast.Constant):
        return arg.value == rev.SUBCOLLECTION
    return (
        isinstance(arg, ast.Attribute) and arg.attr == "SUBCOLLECTION"
        and isinstance(arg.value, ast.Name)
        and arg.value.id in ("revision", "rev", "revision_model")
    )


def test_no_other_module_reaches_the_revisions_subcollection():
    """The deletion of a note's revisions is Lot 1's DECISION (purge or keep
    orphaned). Until it is taken and recorded, nothing may address the
    subcollection except this module — so it cannot be taken by accident."""
    offenders = []
    scanned = 0
    for path in ATHENA.rglob("*.py"):
        rel = path.relative_to(ATHENA).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/")) or "/site-packages/" in rel:
            continue
        if rel == "models/revision.py":
            continue
        scanned += 1
        for node in ast.walk(_module_tree(path)):
            if _reaches_the_subcollection(node):
                offenders.append(f"{rel}:{node.lineno}")
    assert scanned > 50, "the sweep did not walk the source tree"
    assert offenders == []


@pytest.mark.parametrize("snippet, flagged", [
    ('db.collection("notes").document(i).collection("revisions")', True),
    ('ref.collection(revision.SUBCOLLECTION).document(r).delete()', True),
    ('ref.collection(rev.SUBCOLLECTION)', True),
    ('ref.collection("analyses")', False),
    ('ref.collection(SUBCOLLECTION)', False),   # another module's constant
    ('merged["revisions"] = revisions[-25:]', False),  # admin's FIELD
])
def test_the_subcollection_sweep_catches_what_it_claims(snippet, flagged):
    tree = ast.parse(snippet)
    assert any(_reaches_the_subcollection(n) for n in ast.walk(tree)) is flagged
