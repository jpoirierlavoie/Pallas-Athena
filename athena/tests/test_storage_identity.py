"""L'identité de stockage (règle 8 du plan) — ``utils/storage_identity.py``.

Chaque objet Storage vit sous un préfixe clé par uid (``users/{uid}/…``,
``staging/{uid}/…``). Les routes lisaient ce uid par
``session.get("user_id", "unknown")`` et ``doc_template.update_template``
retombait sur ``"unknown"`` quand le chemin stocké était malformé : un
appelant sans session aurait rangé les fichiers du cabinet sous
``users/unknown/``, en silence. On épingle :

1. ``owner_uid`` — mémorisé par processus, CLÉ sur le courriel, fail-closed,
   jamais ``"unknown"`` ;
2. ``request_uid`` — la session d'abord, validée, jamais troquée ;
3. ``require_uid`` — les refus ;
4. les gardes des MODÈLES — refus avant toute E/S ;
5. les routes — un uid de session invalide refuse au lieu d'écrire ;
6. un balayage DÉRIVÉ des constructeurs de chemins de ``routes/``,
   ``models/``, ``services/`` et ``mcp/``.
"""

import ast
import io
import os
import pathlib
import sys
import types
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
    import models.document as doc
    import routes.documents as rd
    from config import Config

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from utils import storage_identity as si  # noqa: E402

ATHENA = pathlib.Path(__file__).resolve().parent.parent
REAL_UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"   # the shape of a Firebase uid


@pytest.fixture(autouse=True)
def _fresh_cache():
    si._reset_cache()
    yield
    si._reset_cache()


class _Auth:
    """A stand-in for ``firebase_admin.auth`` that records its lookups."""

    def __init__(self, uid=REAL_UID, error=None):
        self.uid, self.error, self.calls = uid, error, []

    def get_user_by_email(self, email):
        self.calls.append(email)
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(uid=self.uid(email) if callable(self.uid) else self.uid)


@pytest.fixture()
def fb(monkeypatch):
    """Install a recording ``firebase_admin.auth`` for the lazy import."""
    import firebase_admin

    def _install(**kwargs):
        stub = _Auth(**kwargs)
        monkeypatch.setitem(sys.modules, "firebase_admin.auth", stub)
        monkeypatch.setattr(firebase_admin, "auth", stub, raising=False)
        return stub

    return _install


# ── 1. owner_uid ───────────────────────────────────────────────────────────


def test_the_owner_uid_is_looked_up_once_and_memoized(fb, monkeypatch):
    monkeypatch.setattr(Config, "AUTHORIZED_USER_EMAIL", "  Me@Cabinet.CA ")
    auth = fb()
    assert si.owner_uid() == REAL_UID
    assert si.owner_uid() == REAL_UID
    assert auth.calls == ["me@cabinet.ca"]   # normalized, and only once


def test_the_memo_is_keyed_on_the_email(fb, monkeypatch):
    """A configuration change — or a test — must never read the previous
    owner's uid."""
    auth = fb(uid=lambda email: "uid-" + email.split("@")[0])
    monkeypatch.setattr(Config, "AUTHORIZED_USER_EMAIL", "a@x.ca")
    assert si.owner_uid() == "uid-a"
    monkeypatch.setattr(Config, "AUTHORIZED_USER_EMAIL", "b@x.ca")
    assert si.owner_uid() == "uid-b"
    monkeypatch.setattr(Config, "AUTHORIZED_USER_EMAIL", "a@x.ca")
    assert si.owner_uid() == "uid-a"
    assert auth.calls == ["a@x.ca", "b@x.ca", "a@x.ca"]


def test_a_failed_lookup_raises_and_is_not_cached(fb, monkeypatch):
    monkeypatch.setattr(Config, "AUTHORIZED_USER_EMAIL", "me@cabinet.ca")
    logged = []
    monkeypatch.setattr(si, "log_unexpected", lambda msg, **kw: logged.append(msg))
    auth = fb(error=RuntimeError("auth down"))
    with pytest.raises(si.StorageIdentityUnavailable) as exc:
        si.owner_uid()
    assert "aucun fichier n'a été écrit" in str(exc.value)
    assert logged and all("cabinet" not in m for m in logged)  # never the email
    # Not cached: the next call asks again, and succeeds once the store does.
    auth.error = None
    assert si.owner_uid() == REAL_UID
    assert len(auth.calls) == 2


@pytest.mark.parametrize("bad", ["", None, "unknown", "a/b"])
def test_an_unusable_uid_from_the_lookup_is_refused(fb, monkeypatch, bad):
    monkeypatch.setattr(Config, "AUTHORIZED_USER_EMAIL", "me@cabinet.ca")
    monkeypatch.setattr(si, "log_unexpected", lambda *a, **kw: None)
    fb(uid=bad)
    with pytest.raises(si.StorageIdentityUnavailable):
        si.owner_uid()


def test_an_unconfigured_email_never_reaches_the_lookup(fb, monkeypatch):
    monkeypatch.setattr(Config, "AUTHORIZED_USER_EMAIL", "   ")
    auth = fb()
    with pytest.raises(si.StorageIdentityUnavailable):
        si.owner_uid()
    assert auth.calls == []


def test_the_stubbed_empty_auth_module_fails_closed(monkeypatch):
    """Several test modules install ``firebase_admin.auth`` as an EMPTY
    module; the lazy import then finds no ``get_user_by_email``. That must
    read as « unavailable », never as a uid."""
    import firebase_admin

    empty = types.ModuleType("firebase_admin.auth")
    monkeypatch.setitem(sys.modules, "firebase_admin.auth", empty)
    monkeypatch.setattr(firebase_admin, "auth", empty, raising=False)
    monkeypatch.setattr(si, "log_unexpected", lambda *a, **kw: None)
    with pytest.raises(si.StorageIdentityUnavailable):
        si.owner_uid()


# ── 2. request_uid ─────────────────────────────────────────────────────────


def _request(**session_values):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "t"
    ctx = app.test_request_context("/")
    ctx.push()
    from flask import session
    session.update(session_values)
    return ctx


def test_the_session_uid_wins_and_no_lookup_runs(fb):
    auth = fb()
    ctx = _request(user_id="u-session")
    try:
        assert si.request_uid() == "u-session"
    finally:
        ctx.pop()
    assert auth.calls == []


def test_without_a_session_uid_the_owner_is_used(fb):
    auth = fb()
    ctx = _request()
    try:
        assert si.request_uid() == REAL_UID
    finally:
        ctx.pop()
    assert len(auth.calls) == 1


def test_outside_a_request_the_owner_is_used(fb):
    fb()
    assert si.request_uid() == REAL_UID


@pytest.mark.parametrize("bad", ["unknown", "", "a/b"])
def test_an_invalid_session_uid_raises_and_is_never_swapped(fb, bad):
    """Swapping it for the owner's would split one session's uploads over
    two prefixes (its staging objects under one, the check under another)."""
    auth = fb()
    ctx = _request(user_id=bad)
    try:
        with pytest.raises(si.InvalidStorageUid):
            si.request_uid()
    finally:
        ctx.pop()
    assert auth.calls == []


# ── 3. require_uid ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "", None, "unknown", "Unknown", "UNKNOWN", "None", "null", "undefined",
    "a/b", "/u1", "u1/", "a\\b", ".", "..", " u1", "u1 ", "u 1", "u1\n",
    "u1\x00", "x" * 129, 123, b"u1", ["u1"],
])
def test_require_uid_refuses(bad):
    with pytest.raises(si.InvalidStorageUid) as exc:
        si.require_uid(bad)
    assert "aucun fichier n'a été écrit" in str(exc.value)


@pytest.mark.parametrize("good", ["u1", REAL_UID, "x" * 128, "abc-DEF_123"])
def test_require_uid_accepts_a_real_uid(good):
    assert si.require_uid(good) == good


def test_the_refusal_is_the_unavailable_family():
    """One except clause catches both: a caller has one failure to handle."""
    assert issubclass(si.InvalidStorageUid, si.StorageIdentityUnavailable)
    assert issubclass(si.StorageIdentityUnavailable, RuntimeError)


# ── 4. Les gardes des modèles — refus AVANT toute E/S ─────────────────────


def _no_io(monkeypatch, module):
    """Any Storage or Firestore touch fails the test."""
    def _forbidden(*_a, **_kw):
        raise AssertionError("I/O despite an invalid uid")

    monkeypatch.setattr(module.storage, "bucket", _forbidden)
    monkeypatch.setattr(module, "db", mock.Mock(collection=_forbidden))


class _Stream(io.BytesIO):
    def read(self, *a):
        raise AssertionError("stream read despite an invalid uid")


def test_upload_document_refuses_an_unusable_uid(monkeypatch):
    _no_io(monkeypatch, doc)
    got, errors = doc.upload_document(
        "d1", "2026-001", _Stream(b"%PDF-1.7"), "a.pdf", 8, {}, "unknown",
    )
    assert got is None and errors == [si.INVALID_UID_MESSAGE]


def test_ingest_refuses_an_unusable_uid_before_the_probe(monkeypatch):
    _no_io(monkeypatch, doc)
    source = mock.Mock(size=8)
    source.download_as_bytes.side_effect = AssertionError("probe read")
    got, errors = doc.ingest_blob_as_document(
        source, "d1", "2026-001", "a.pdf", {}, "unknown",
    )
    assert got is None and errors == [si.INVALID_UID_MESSAGE]


def test_the_shared_path_builder_guards_the_uid_itself(monkeypatch):
    _no_io(monkeypatch, doc)
    got, errors = doc._prepare_document_record(
        "d1", "2026-001", "a.pdf", ".pdf", "application/pdf", 8, {}, "a/b",
    )
    assert got is None and errors == [si.INVALID_UID_MESSAGE]


def test_the_zip_export_refuses_an_unusable_uid(monkeypatch):
    _no_io(monkeypatch, doc)
    import models.dossier as dossier_model

    monkeypatch.setattr(dossier_model, "get_dossier",
                        lambda *_a: pytest.fail("read the dossier"))
    url, errors = doc.build_folder_zip_url("d1", None, "unknown")
    assert url is None and errors == [si.INVALID_UID_MESSAGE]


def test_create_template_refuses_before_reading_the_stream(monkeypatch):
    _no_io(monkeypatch, tpl)
    got, errors = tpl.create_template(
        _Stream(b""), "modele.docx", 10, {"name": "Lettre"}, "unknown",
    )
    assert got is None and errors == [si.INVALID_UID_MESSAGE]


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
_DOCUMENT = (
    '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.'
    'org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{{tribunal}}'
    '</w:t></w:r></w:p></w:body></w:document>'
)


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("word/document.xml", _DOCUMENT)
    return buf.getvalue()


@pytest.fixture()
def gabarits(monkeypatch):
    """A stored template on the shared fake Firestore + the fake bucket.

    Rewritten deliberately with lot 2A, step T3: the bucket used to be a
    MagicMock that recorded calls; it is now the realistic fake, so the
    tests below assert what the bucket HOLDS (the replaced file is kept)."""
    fake = install(monkeypatch, tpl)
    bucket = FakeBucket()
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)

    def _seed(storage_path):
        fake.seed("doc_templates/t1", {
            "id": "t1", "name": "Lettre", "category": "correspondance",
            "kind": "gabarit", "filename": "ancien.docx",
            "storage_path": storage_path, "version": 1, "etag": "e1",
        })
        if storage_path:
            bucket.put(storage_path, b"old")

    return types.SimpleNamespace(fake=fake, seed=_seed, bucket=bucket)


@pytest.mark.parametrize("stored", [
    "users/unknown/templates/t1/ancien.docx",   # the old fallback's residue
    "users/None/templates/t1/ancien.docx",
    "t1/ancien.docx",                          # the old fallback's trigger
    "",
    "staging/u1/templates/t1/ancien.docx",     # not the users/ layout
    "users/u1/dossiers/t1/ancien.docx",
])
def test_update_template_refuses_a_stored_path_that_names_no_owner(
    gabarits, stored
):
    gabarits.seed(stored)
    before = dict(gabarits.bucket.objects)
    got, errors, changed = tpl.update_template(
        "t1", {}, file_stream=_Stream(b""), filename="nouveau.docx",
        file_size=10,
    )
    assert got is None and errors == [tpl.TEMPLATE_PATH_INVALID_MESSAGE]
    assert changed is False
    assert gabarits.bucket.objects == before     # nothing written, nothing deleted
    assert gabarits.fake.peek("doc_templates/t1")["storage_path"] == stored


def test_update_template_replaces_the_file_under_the_same_owner(gabarits):
    """The happy path still works — and still writes under the STORED uid.

    Rewritten deliberately with lot 2A, step T3 (D11): the new file lands
    at its own ``v2`` path, and the replaced one is KEPT (it used to be
    deleted — the previous version was lost)."""
    old_path = f"users/{REAL_UID}/templates/t1/ancien.docx"
    gabarits.seed(old_path)
    payload = _docx()
    got, errors, changed = tpl.update_template(
        "t1", {}, file_stream=io.BytesIO(payload), filename="nouveau.docx",
        file_size=len(payload),
    )
    assert errors == [] and changed is True
    new_path = f"users/{REAL_UID}/templates/t1/v2/nouveau.docx"
    assert got["storage_path"] == new_path and got["version"] == 2
    assert gabarits.bucket.objects[new_path].data == payload
    assert gabarits.bucket.objects[old_path].data == b"old"   # kept
    assert gabarits.fake.peek("doc_templates/t1")["storage_path"] == new_path


def test_a_metadata_only_update_needs_no_owner_segment(gabarits):
    """No file → no path is built, so a legacy path does not block a rename."""
    gabarits.seed("users/unknown/templates/t1/ancien.docx")
    before = dict(gabarits.bucket.objects)
    got, errors, changed = tpl.update_template("t1", {"name": "Lettre (révisée)"})
    assert errors == [] and changed is True
    assert got["name"] == "Lettre (révisée)"
    assert gabarits.bucket.objects == before


# ── 5. Les routes ──────────────────────────────────────────────────────────


def _web(user_id):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _forbid_storage(monkeypatch):
    monkeypatch.setattr(rd.storage, "bucket",
                        lambda: pytest.fail("opened a GCS session"))


def test_an_invalid_session_uid_refuses_the_upload_session(monkeypatch):
    """``@login_required`` only checks that the uid is TRUTHY: « unknown »
    passes it. The route must refuse rather than open staging/unknown/."""
    _forbid_storage(monkeypatch)
    resp = _web("unknown").post("/documents/api/televersement", json={
        "name": "a.pdf", "size": 100,
    })
    assert resp.status_code == 503
    assert resp.get_json()["erreur"] == si.INVALID_UID_MESSAGE


def test_an_invalid_session_uid_refuses_the_finalisation(monkeypatch):
    _forbid_storage(monkeypatch)
    resp = _web("unknown").post("/documents/api/finaliser", json={
        "objet": "staging/unknown/x/a.pdf", "name": "a.pdf", "dossier_id": "d1",
    })
    assert resp.status_code == 503


def test_an_invalid_session_uid_refuses_the_zip_with_a_banner(monkeypatch):
    monkeypatch.setattr(rd, "build_folder_zip_url",
                        lambda *a: pytest.fail("built a zip path"))
    resp = _web("unknown").get("/documents/zip?dossier_id=d1")
    assert resp.status_code == 302
    assert "erreur=" in resp.headers["Location"]


def test_a_valid_session_uid_still_names_the_staging_prefix(monkeypatch):
    blob = mock.Mock()
    blob.create_resumable_upload_session.return_value = "https://up.example/s"
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    resp = _web(REAL_UID).post("/documents/api/televersement", json={
        "name": "a.pdf", "size": 100,
    })
    assert resp.status_code == 200
    assert resp.get_json()["objet"].startswith(f"staging/{REAL_UID}/")


# ── 6. Le balayage dérivé ─────────────────────────────────────────────────

_SCANNED_PACKAGES = ("routes", "models", "services", "mcp")
_STORAGE_PREFIXES = ("users/", "staging/")
_GUARDS = {"require_uid", "request_uid", "owner_uid"}


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                names.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                names.add(sub.func.attr)
    return names


def _builds_a_storage_path(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.JoinedStr) and sub.values:
            head = sub.values[0]
            if (isinstance(head, ast.Constant) and isinstance(head.value, str)
                    and head.value.startswith(_STORAGE_PREFIXES)):
                return True
    return False


def _functions(tree: ast.Module):
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def storage_uid_violations(source: str, label: str) -> tuple[list[str], list[str]]:
    """(violations, builders) of one module's source.

    A BUILDER is a function holding an f-string that starts with a
    uid-keyed Storage prefix. Each must call a storage-identity guard —
    directly, or through a function of the same module that does (derived,
    never an allowlist) — and none may carry an ``"unknown"`` literal nor
    read the session uid with a default (``session.get("user_id", …)``:
    whatever the default, it is a uid nobody verified). Anywhere in the
    module: never the idiom itself, ``session.get("user_id", "unknown")``.
    (A non-builder may read the session uid with a default for DISPLAY —
    ``routes/settings.securite`` hands it to the page as an identity check
    and builds no path with it.)
    """
    tree = ast.parse(source)
    functions = _functions(tree)
    local_guards = {f.name for f in functions if _called_names(f) & _GUARDS}
    guards = _GUARDS | local_guards
    violations, builders = [], []
    for fn in functions:
        if not _builds_a_storage_path(fn):
            continue
        builders.append(f"{label}:{fn.name}")
        if not (_called_names(fn) & guards):
            violations.append(f"{label}:{fn.name} builds a path without a uid guard")
        for sub in ast.walk(fn):
            if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    and sub.value.strip().lower() == "unknown"):
                violations.append(f"{label}:{fn.name}:{sub.lineno} « unknown » literal")
            if _session_uid_default(sub) is not None:
                violations.append(
                    f"{label}:{fn.name}:{sub.lineno} session uid read with a default"
                )
    for sub in ast.walk(tree):
        default = _session_uid_default(sub)
        if (isinstance(default, ast.Constant) and isinstance(default.value, str)
                and default.value.strip().lower() == "unknown"):
            violations.append(f"{label}:{sub.lineno} session.get('user_id', 'unknown')")
    return violations, builders


def _session_uid_default(node: ast.AST):
    """The default of a ``session.get("user_id", <default>)`` call, else None."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and len(node.args) >= 2
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "session"
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "user_id"):
        return node.args[1]
    return None


def _scan():
    violations, builders = [], []
    for package in _SCANNED_PACKAGES:
        for path in sorted((ATHENA / package).rglob("*.py")):
            label = path.relative_to(ATHENA).as_posix()
            v, b = storage_uid_violations(path.read_text(encoding="utf-8"), label)
            violations += v
            builders += b
    return violations, builders


def test_no_storage_path_builder_can_write_under_an_unknown_uid():
    violations, _ = _scan()
    assert violations == []


def test_the_sweep_finds_the_builders_it_exists_for():
    """Not vacuous: every known builder is found — a refactor that moves one
    out of reach of the sweep fails here, not silently."""
    _, builders = _scan()
    for expected in (
        "models/document.py:_prepare_document_record",
        "models/document.py:build_folder_zip_url",
        # Lot 2A, step T3: the gabarit paths are built in ONE place — every
        # version's own ``v{N}`` object — which create, update and restore
        # all call (the two entries it replaces named the two functions
        # that each held their own f-string).
        "models/doc_template.py:_template_object_path",
        "routes/documents.py:api_televersement",
        "routes/documents.py:api_finaliser",
        "routes/admin_ledger.py:api_televersement",
        "routes/admin_ledger.py:api_recu",
    ):
        assert expected in builders, expected


@pytest.mark.parametrize("snippet, flagged", [
    ('def f():\n    uid = session.get("user_id", "unknown")\n', True),   # the idiom
    ('def f():\n    uid = session.get("user_id", "")\n'
     '    uid = require_uid(uid)\n    return f"users/{uid}/x"\n', True),  # in a builder
    ('def f():\n    return session.get("user_id", "")\n', False),   # display only
    ('def f(u):\n    return f"users/{u}/x"\n', True),                  # no guard
    ('def f(p):\n    s = p[1] if p else "unknown"\n'
     '    s = require_uid(s)\n    return f"users/{s}/x"\n', True),     # literal
    ('def f(u):\n    u = require_uid(u)\n    return f"users/{u}/x"\n', False),
    ('def g(u):\n    return require_uid(u)\n'
     'def f(u):\n    u = g(u)\n    return f"staging/{u}/x"\n', False),  # derived guard
    ('def f():\n    return session.get("user_id")\n', False),
    ('def f(ip):\n    return ip or "unknown"\n', False),               # not a builder
    ('def f(i):\n    return f"submissions/{i}/x"\n', False),           # not uid-keyed
])
def test_the_sweep_catches_what_it_claims(snippet, flagged):
    violations, _ = storage_uid_violations(snippet, "snippet")
    assert bool(violations) is flagged


# ── 7. Les APPELANTS des écrivains qui prennent un uid ────────────────────
#
# The builder sweep above sees the f-string, so it cannot see a route that
# hands a raw session uid to a MODEL builder (routes/reception.verser did,
# with ``session["user_id"]``, until the step-7 review). The model re-checks
# the uid — but only when it runs, which in « Verser » came AFTER
# get_or_create_folder had already written the « Reçus du portail » folder.
# So: every function of routes/, services/ or mcp/ that calls a model writer
# taking a ``user_id`` must itself obtain it from a guard. Both inventories
# are DERIVED — the writers from models/, the callers from the call sites.

_CALLER_PACKAGES = ("routes", "services", "mcp")


def _module_guards(tree: ast.Module) -> set[str]:
    return _GUARDS | {f.name for f in _functions(tree)
                      if _called_names(f) & _GUARDS}


def _uid_taking_writers() -> dict[str, str]:
    """{function name: label} — the models/ functions that take a
    ``user_id`` parameter and guard it (directly or through a module-local
    guard such as ``document._storage_uid``)."""
    writers = {}
    for path in sorted((ATHENA / "models").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guards = _module_guards(tree)
        for fn in _functions(tree):
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            if "user_id" in params and _called_names(fn) & guards:
                writers[fn.name] = f"{path.relative_to(ATHENA).as_posix()}:{fn.name}"
    return writers


def uid_caller_violations(source: str, label: str, writers) -> tuple[list[str], list[str]]:
    """(violations, callers) of one module: a caller of a uid-taking writer
    that does not call a storage-identity guard itself."""
    tree = ast.parse(source)
    guards = _module_guards(tree)
    violations, callers = [], []
    for fn in _functions(tree):
        called = _called_names(fn)
        if not called & set(writers):
            continue
        callers.append(f"{label}:{fn.name}")
        if not called & guards:
            violations.append(
                f"{label}:{fn.name} hands a uid to "
                f"{sorted(called & set(writers))} without a storage guard"
            )
    return violations, callers


def _caller_scan():
    writers = _uid_taking_writers()
    violations, callers = [], []
    for package in _CALLER_PACKAGES:
        for path in sorted((ATHENA / package).rglob("*.py")):
            label = path.relative_to(ATHENA).as_posix()
            v, c = uid_caller_violations(
                path.read_text(encoding="utf-8"), label, writers,
            )
            violations += v
            callers += c
    return writers, violations, callers


def test_every_caller_of_a_uid_taking_writer_obtains_the_uid_from_a_guard():
    _, violations, _ = _caller_scan()
    assert violations == []


def test_the_caller_sweep_finds_what_it_exists_for():
    writers, _, callers = _caller_scan()
    for expected in ("upload_document", "ingest_blob_as_document",
                     "build_folder_zip_url", "create_template"):
        assert expected in writers, expected
    # update_template takes no uid (it reuses the STORED one): not a writer.
    assert "update_template" not in writers
    for expected in (
        "routes/documents.py:folder_zip",
        "routes/documents.py:api_finaliser",
        "routes/doc_templates.py:template_create",
        "routes/doc_templates.py:generate",
        "routes/invoices.py:invoice_note_docx",
        "routes/reception.py:verser",
    ):
        assert expected in callers, expected


@pytest.mark.parametrize("snippet, flagged", [
    ('def v():\n    ingest_blob_as_document(b, "d", "n", "f", {}, session["user_id"])\n',
     True),                                                  # the reception shape
    ('def v():\n    uid = storage_identity.request_uid()\n'
     '    doc.upload_document("d", "n", s, "f", 1, {}, uid)\n', False),
    ('def v():\n    upload_document("d", "n", s, "f", 1, {}, owner_uid())\n', False),
    ('def v():\n    list_documents("d")\n', False),         # not a uid writer
])
def test_the_caller_sweep_catches_what_it_claims(snippet, flagged):
    writers = {"ingest_blob_as_document": "m", "upload_document": "m"}
    violations, _ = uid_caller_violations(snippet, "snippet", writers)
    assert bool(violations) is flagged


def test_the_idiom_is_absent_from_the_whole_source_tree():
    """The builder sweep scans four packages; the idiom itself is refused in
    EVERY non-test module (scripts/, utils/, dav/, client/ included), so a
    future helper cannot reintroduce it one directory over."""
    offenders, scanned = [], 0
    for path in sorted(ATHENA.rglob("*.py")):
        rel = path.relative_to(ATHENA).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/")) or "/site-packages/" in rel:
            continue
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            default = _session_uid_default(node)
            if (isinstance(default, ast.Constant) and isinstance(default.value, str)
                    and default.value.strip().lower() == "unknown"):
                offenders.append(f"{rel}:{node.lineno}")
    assert scanned > 50, "the sweep did not walk the source tree"
    assert offenders == []
