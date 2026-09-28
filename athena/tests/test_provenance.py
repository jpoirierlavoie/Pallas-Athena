"""La provenance — QUI a écrit un enregistrement en dernier, estampillée par
le modèle qui l'écrit (plan lot 0a, règle transversale 5).

Ce que ces tests épinglent, dans l'ordre d'importance :

1. Le tampon suit l'etag. Toute instruction de ``models/*.py`` qui écrit un
   ``etag`` écrit aussi ``updated_via`` : un ``updated_via`` posé par un seul
   appelant survivrait, périmé, à l'écriture SUIVANTE de n'importe qui (les
   modèles fusionnent ``{**existing, **data}``), et la bannière de conflit
   dirait « modifié par Claude » d'une écriture du fidéicommis. Balayage
   DÉRIVÉ du source, pas une liste.
2. Le chemin d'écriture est dérivé, jamais déclaré par l'appelant : la
   surcharge de ``run_write`` pour le connecteur, sinon le blueprint de la
   requête — classé ici à partir des blueprints RÉELLEMENT enregistrés par
   ``main.create_app()``, par des signaux indépendants de l'implémentation
   (exemption CSRF, espace d'URL, appartenance au paquet du connecteur).
3. Chaque mutateur que le connecteur atteint estampille ET note son point de
   validation (``note_commit``) après son écriture — dérivé des références
   ``<x>_model.<verbe>`` de ``mcp/handlers.py`` ET des modules ``services/``
   qu'il importe (lot 1b) ; un mutateur qui délègue toutes ses écritures est
   jugé sur ses délégués.
4. Le vrai magasin : les modèles tournent au-dessus du faux Firestore partagé
   (``tests/_fake_firestore.py`` — le client est le vrai), et l'on relit ce
   qui est STOCKÉ, jamais un dictionnaire remis à un mock.
"""

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Blueprint, Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.write_support as write_support
    from models import invoice as invoice_model
    from models import note as note_model
    from models import provenance
    from models import task as task_model
    from models import time_entry as time_entry_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 26, 9, 30, tzinfo=UTC)

ATHENA = pathlib.Path(__file__).resolve().parents[1]
MODELS = ATHENA / "models"


# ══════════════════════════════════════════════════════════════════════
# 1. Le module — surcharge, tampons, registre de validation
# ══════════════════════════════════════════════════════════════════════


def test_without_a_request_or_an_override_the_writer_is_a_script():
    assert provenance.current_via() == "script"
    assert provenance.current_tool() == ""


def test_the_vocabulary_is_closed_and_ordered():
    assert provenance.VALID_VIA == ("web", "dav", "mcp", "cron", "script")
    with pytest.raises(ValueError):
        with provenance.writing_via("claude"):
            pass  # pragma: no cover


def test_writing_via_overrides_and_restores_even_on_exception():
    with provenance.writing_via("mcp", tool="create_task"):
        assert provenance.current_via() == "mcp"
        assert provenance.current_tool() == "create_task"
        with provenance.writing_via("cron"):
            assert provenance.current_via() == "cron"
            assert provenance.current_tool() == ""
        assert provenance.current_via() == "mcp"
    assert provenance.current_via() == "script"

    with pytest.raises(RuntimeError):
        with provenance.writing_via("mcp", tool="x"):
            raise RuntimeError("boom")
    # A thread serving the next request must never inherit the override.
    assert provenance.current_via() == "script"
    assert provenance.current_tool() == ""


def test_update_fields_outside_the_connector_carries_no_mcp_instant():
    fields = provenance.update_fields(NOW)
    assert set(fields) == {"updated_at", "etag", "updated_via"}
    assert fields["updated_at"] == NOW and fields["updated_via"] == "script"
    assert fields["etag"] and fields["etag"] != provenance.update_fields(NOW)["etag"]


def test_update_fields_under_the_connector_adds_the_sticky_instant():
    with provenance.writing_via("mcp", tool="update_partie"):
        fields = provenance.update_fields(NOW)
    assert set(fields) == {"updated_at", "etag", "updated_via", "mcp_updated_at"}
    assert fields["updated_via"] == "mcp" and fields["mcp_updated_at"] == NOW


def test_stamp_update_never_clears_mcp_updated_at_nor_touches_creation():
    doc = {"created_at": NOW, "created_via": "mcp", "etag": "old",
           "updated_via": "mcp", "mcp_updated_at": NOW}
    provenance.stamp_update(doc, LATER)  # a script (then a web save) writes
    assert doc["updated_via"] == "script"
    assert doc["mcp_updated_at"] == NOW  # sticky — « Claude a modifié le … »
    assert doc["created_at"] == NOW and doc["created_via"] == "mcp"
    assert doc["etag"] != "old" and doc["updated_at"] == LATER


def test_stamp_create_keeps_a_valid_explicit_created_via_only():
    kept = provenance.stamp_create({"created_via": "mcp"}, NOW)
    assert kept["created_via"] == "mcp" and kept["updated_via"] == "script"

    for bogus in ("", None, "claude", "import"):
        doc = provenance.stamp_create({"created_via": bogus}, NOW)
        assert doc["created_via"] == "script", bogus

    with provenance.writing_via("mcp"):
        doc = provenance.stamp_create({}, NOW)
    assert doc["created_via"] == doc["updated_via"] == "mcp"
    assert doc["mcp_updated_at"] == NOW
    assert doc["created_at"] == doc["updated_at"] == NOW


def test_stamp_create_preserves_only_a_creation_instant_passed_explicitly():
    doc = provenance.stamp_create({"created_at": LATER}, NOW)
    assert doc["created_at"] == NOW  # never implicitly
    doc = provenance.stamp_create({}, NOW, created_at=LATER)
    assert doc["created_at"] == LATER and doc["updated_at"] == NOW


def test_create_fields_is_update_fields_plus_the_creation_pair():
    with provenance.writing_via("cron"):
        fields = provenance.create_fields(NOW)
    assert set(fields) == {"created_at", "created_via", "updated_at",
                           "etag", "updated_via"}
    assert fields["created_via"] == fields["updated_via"] == "cron"


def test_note_commit_records_only_inside_a_writing_block():
    provenance.note_commit("tasks", "orphan")  # no block: nothing kept
    assert provenance.committed_writes() == ()
    with provenance.writing_via("mcp", tool="t"):
        assert provenance.committed_writes() == ()
        provenance.note_commit("tasks", "t1")
        provenance.note_commit("notes", "n1")
        seen = provenance.committed_writes()
        assert seen == (("tasks", "t1"), ("notes", "n1"))
        with provenance.writing_via("mcp", tool="inner"):
            assert provenance.committed_writes() == ()  # a fresh record
            provenance.note_commit("hearings", "h1")
        # The inner commit happened inside the outer block too: handed up,
        # never forgotten (a forgotten commit reads « nothing written »).
        assert provenance.committed_writes() == seen + (("hearings", "h1"),)
    assert provenance.committed_writes() == ()


def test_a_nested_block_that_raises_still_hands_its_commits_up():
    """The exception path is the one the commit record exists for: the
    outer write protocol must still learn that the inner write committed."""
    with provenance.writing_via("mcp", tool="outer"):
        with pytest.raises(RuntimeError):
            with provenance.writing_via("cron"):
                provenance.note_commit("tasks", "t9")
                raise RuntimeError("after the commit")
        assert provenance.committed_writes() == (("tasks", "t9"),)
        assert provenance.current_via() == "mcp"  # override restored
    assert provenance.committed_writes() == ()


def test_committed_writes_is_a_copy_the_reader_cannot_edit():
    with provenance.writing_via("mcp"):
        provenance.note_commit("tasks", "t1")
        snapshot = provenance.committed_writes()
        assert isinstance(snapshot, tuple)
        provenance.note_commit("tasks", "t2")
        assert snapshot == (("tasks", "t1"),)


# ══════════════════════════════════════════════════════════════════════
# 2. Le chemin dérivé du blueprint
# ══════════════════════════════════════════════════════════════════════


def _probe_app() -> Flask:
    """A Flask app whose blueprints live, by import name, where the real
    ones do. ``root_path`` is given so Flask never imports those modules."""
    app = Flask(__name__)
    for name, import_name in (
        ("p_dav", "dav"),
        ("p_carddav", "dav.carddav"),
        ("p_cron", "routes.taches_bookings"),
        ("p_tasks", "routes.tasks"),  # /taches, but the WEB task list
        ("p_web", "routes.parties"),
        ("p_mcp", "mcp"),
    ):
        bp = Blueprint(name, import_name, root_path=str(ATHENA))
        bp.add_url_rule(f"/{name}", name, lambda: "")
        app.register_blueprint(bp)
    app.add_url_rule("/plain", "plain", lambda: "")
    return app


@pytest.mark.parametrize("path, expected", [
    ("/p_dav", "dav"), ("/p_carddav", "dav"), ("/p_cron", "cron"),
    ("/p_tasks", "web"), ("/p_web", "web"), ("/p_mcp", "mcp"),
    ("/plain", "web"), ("/nowhere", "web"),
])
def test_the_request_blueprint_decides_the_path(path, expected):
    app = _probe_app()
    with app.test_request_context(path):
        assert provenance.current_via() == expected


def test_the_override_wins_over_the_request():
    app = _probe_app()
    with app.test_request_context("/p_dav"):
        with provenance.writing_via("mcp", tool="x"):
            assert provenance.current_via() == "mcp"
        assert provenance.current_via() == "dav"


_REAL_APP_PROBE = textwrap.dedent('''
    import json, os, re, sys
    from unittest import mock
    sys.path.insert(0, os.getcwd())
    with mock.patch("google.cloud.firestore.Client"):
        import main
        import mcp
        from models import provenance
        from security import csrf
    from flask import request

    app = main.app
    exempt = {getattr(b, "name", b) for b in csrf._exempt_blueprints}
    connector = {mcp.mcp_bp.name, mcp.oauth_bp.name}
    out = {}
    for name in app.blueprints:
        rules = [r for r in app.url_map.iter_rules()
                 if r.endpoint.startswith(name + ".")]
        probes = []
        for r in rules:
            path = re.sub(r"<[^>]+>", "x", r.rule)
            methods = sorted(r.methods - {"HEAD", "OPTIONS"}) or ["OPTIONS"]
            with app.test_request_context(path, method=methods[0]):
                probes.append({"matched": request.blueprint,
                               "via": provenance.current_via()})
        out[name] = {"csrf_exempt": name in exempt,
                     "connector": name in connector,
                     "paths": [r.rule for r in rules],
                     "probes": probes}
    with app.test_request_context("/_ah/warmup"):
        app_level = {"matched": request.blueprint,
                     "via": provenance.current_via()}
    print("@@PROVENANCE@@" + json.dumps({"blueprints": out, "app": app_level}))
''')


@pytest.fixture(scope="module")
def real_app():
    """The REAL ``main.create_app()`` — in a subprocess, because importing
    ``main`` builds the app at module level (root logging handler,
    OpenTelemetry provider, Firebase app): state this suite must not
    inherit."""
    env = {**os.environ}
    env.pop("ENV", None)
    proc = subprocess.run(
        [sys.executable, "-c", _REAL_APP_PROBE],
        cwd=str(ATHENA), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("@@PROVENANCE@@"))
    return json.loads(line[len("@@PROVENANCE@@"):])


def _expected_via(bp: dict) -> str:
    """Classify a registered blueprint WITHOUT the implementation's rule:
    the connector package's two blueprints are the connector; a CSRF-exempt
    blueprint serving ``/dav/`` speaks DAV; a CSRF-exempt blueprint living
    entirely under ``/taches/`` is a machine (Cloud Tasks / cron) endpoint;
    everything else is a browser surface."""
    if bp["connector"]:
        return "mcp"
    if bp["csrf_exempt"] and any(p.startswith("/dav/") for p in bp["paths"]):
        return "dav"
    if bp["csrf_exempt"] and bp["paths"] and all(
        p.startswith("/taches/") for p in bp["paths"]
    ):
        return "cron"
    return "web"


def test_every_registered_blueprint_writes_under_its_derived_path(real_app):
    blueprints = real_app["blueprints"]
    classes = {}
    for name, bp in blueprints.items():
        expected = _expected_via(bp)
        classes.setdefault(expected, []).append(name)
        matched = [p for p in bp["probes"] if p["matched"] == name]
        assert matched, f"{name}: no rule of it could be requested"
        for probe in matched:
            assert probe["via"] == expected, (name, probe, expected)
    # Not vacuous: each class exists in the real app.
    assert {"web", "dav", "cron", "mcp"} <= set(classes), classes
    # The web task list lives under /taches too, and is NOT a machine.
    assert "tasks" in classes["web"]


def test_every_dav_url_is_served_by_a_dav_classified_blueprint(real_app):
    for name, bp in real_app["blueprints"].items():
        if any(p.startswith("/dav/") for p in bp["paths"]):
            assert _expected_via(bp) == "dav", name


def test_an_app_level_route_is_web(real_app):
    assert real_app["app"] == {"matched": None, "via": "web"}


# ══════════════════════════════════════════════════════════════════════
# 3. run_write déclare le connecteur
# ══════════════════════════════════════════════════════════════════════


def test_run_write_executes_under_the_connector_and_resets(monkeypatch):
    install(monkeypatch, write_support)
    seen = {}

    def execute():
        seen["via"] = provenance.current_via()
        seen["tool"] = provenance.current_tool()
        provenance.note_commit("tasks", "t1")
        seen["commits"] = provenance.committed_writes()
        return {"ok": True}

    payload = write_support.run_write("create_task", {}, execute)
    assert payload == {"ok": True, "idempotent_replay": False}
    assert seen == {"via": "mcp", "tool": "create_task",
                    "commits": (("tasks", "t1"),)}
    assert provenance.current_via() == "script"
    assert provenance.committed_writes() == ()


def test_run_write_resets_the_context_when_execute_raises(monkeypatch):
    install(monkeypatch, write_support)

    def execute():
        raise RuntimeError("after commit, say")

    with pytest.raises(RuntimeError):
        write_support.run_write("create_task", {}, execute)
    assert provenance.current_via() == "script"
    assert provenance.current_tool() == ""


def test_run_write_wraps_execute_in_writing_via_by_construction():
    """The call to ``execute`` sits INSIDE ``with provenance.writing_via(
    "mcp", tool=tool)`` — read from the source, so a refactor that hoists
    it out fails here even if a test happens to pass another way."""
    tree = ast.parse(textwrap.dedent(
        pathlib.Path(write_support.__file__).read_text(encoding="utf-8")))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_write")
    withs = [n for n in ast.walk(fn) if isinstance(n, ast.With)]
    inside = []
    for w in withs:
        for item in w.items:
            call = item.context_expr
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "writing_via"
                    and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value == "mcp"
                    and any(k.arg == "tool" and isinstance(k.value, ast.Name)
                            and k.value.id == "tool" for k in call.keywords)):
                inside += [n for n in ast.walk(w) if isinstance(n, ast.Call)
                           and isinstance(n.func, ast.Name)
                           and n.func.id == "execute"]
    every = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "execute"]
    assert every and len(inside) == len(every), (
        "run_write must call execute() only inside "
        'provenance.writing_via("mcp", tool=tool)'
    )


# ══════════════════════════════════════════════════════════════════════
# 4. Balayage dérivé : un etag écrit emporte son updated_via
# ══════════════════════════════════════════════════════════════════════

# {"relative/path.py:lineno": reason} — an explicit, reasoned exemption for
# a writer that genuinely cannot stamp. None is needed today; a stale entry
# fails below, so the dict can only shrink.
_ETAG_WRITE_EXEMPT: dict[str, str] = {}


def _is_const(node, value) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def etag_write_violations(source: str, label: str) -> list[str]:
    """Every statement of *source* that writes an ``etag`` without writing
    ``updated_via`` alongside it.

    * a dict literal with an ``"etag"`` key (other than the ``""`` default of
      a ``_default_doc``) and no ``"updated_via"`` key;
    * ``x["etag"] = …`` in any form — the only stamping path is
      ``provenance.stamp_update``/``stamp_create``;
    * an ``etag=`` keyword, or ``.setdefault("etag", …)``.

    A ``**provenance.update_fields(now)`` spread carries no literal key, so
    it is compliant by construction.
    """
    out = []
    for node in ast.walk(ast.parse(source)):
        where = f"{label}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Dict):
            keys = [k for k in node.keys if k is not None]
            for key, value in zip(node.keys, node.values):
                if key is not None and _is_const(key, "etag") and not _is_const(value, ""):
                    if not any(_is_const(k, "updated_via") for k in keys):
                        out.append(where)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Subscript) and _is_const(sub.slice, "etag"):
                        out.append(where)
        elif isinstance(node, ast.Call):
            if any(k.arg == "etag" for k in node.keywords):
                out.append(where)
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setdefault"
                    and node.args and _is_const(node.args[0], "etag")):
                out.append(where)
    return out


def _model_files() -> list[pathlib.Path]:
    return sorted(p for p in MODELS.glob("*.py"))


def test_every_model_etag_write_also_writes_updated_via():
    violations = []
    for path in _model_files():
        label = f"models/{path.name}"
        found = etag_write_violations(path.read_text(encoding="utf-8"), label)
        violations += [v for v in found if v not in _ETAG_WRITE_EXEMPT]
    assert not violations, (
        "an etag written without updated_via — the conflict banner would "
        "name the PREVIOUS writer. Use provenance.update_fields / "
        "stamp_update / stamp_create:\n  " + "\n  ".join(violations)
    )


def test_the_etag_exemptions_are_all_still_needed():
    for entry, reason in _ETAG_WRITE_EXEMPT.items():
        assert reason.strip(), entry
        rel, _, _line = entry.partition(":")
        found = etag_write_violations(
            (ATHENA / rel).read_text(encoding="utf-8"), rel)
        assert entry in found, f"{entry}: exemption no longer needed"


@pytest.mark.parametrize("snippet, flagged", [
    ('d = {"updated_at": now, "etag": str(uuid.uuid4())}', True),
    ('txn.update(ref, {"etag": e, "status": "x"})', True),
    ('doc["etag"] = str(uuid.uuid4())', True),
    ('doc["etag"] += "x"', True),
    ('f(etag="x")', True),
    ('doc.setdefault("etag", e)', True),
    ('d = {"etag": e, "updated_via": v}', False),
    ('d = {"etag": ""}', False),  # a _default_doc placeholder
    ('d = {**provenance.update_fields(now), "status": "x"}', False),
    ('x = doc.get("etag")', False),
    ('if a["etag"] != b: pass', False),
])
def test_the_sweep_catches_what_it_claims(snippet, flagged):
    """The guard proves its own mechanism — a sweep that finds nothing on
    real code is only evidence if it finds something on a planted defect."""
    assert bool(etag_write_violations(snippet, "snippet")) is flagged


def test_the_sweep_is_not_vacuous():
    """The models do stamp — through the helpers the sweep points to."""
    helper = re.compile(
        r"provenance\.(update_fields|create_fields|stamp_update|stamp_create)\(")
    counts = {p.name: len(helper.findall(p.read_text(encoding="utf-8")))
              for p in _model_files()}
    assert sum(counts.values()) >= 60, counts
    for name in ("trust.py", "admin_ledger.py", "invoice.py", "protocol.py",
                 "partie.py", "dossier.py", "note.py", "task.py", "hearing.py",
                 "document.py", "time_entry.py", "expense.py"):
        assert counts[name] >= 1, name


# ══════════════════════════════════════════════════════════════════════
# 5. Les mutateurs que le connecteur atteint — dérivés du source
# ══════════════════════════════════════════════════════════════════════

# ``upload``/``ingest``/``copy`` since lot 2A (T8): the connector's
# generations reach the two document creators (through services/gabarits
# and models/document.copy_document), which noted no commit until then — a
# failure after the upload (the payload builder) would have come back as a
# refusal, and the retry saved a second document.
_MUTATOR_VERB = re.compile(
    r"^(create|update|set|record|append|void|reverse|clear|confirm|move|"
    r"delete|toggle|complete|attach|link|add|unlink|ensure|upload|ingest|"
    r"copy)_"
)
# Lot 1 completeness review: lot 1b's handlers reach models THROUGH the
# service modules the web routes also use (``services/protocoles.py``,
# ``services/rendez_vous.py``). A sweep of ``mcp/handlers.py`` alone never
# saw ``protocol.set_step_status`` or ``hearing.set_bookings_confirmation``,
# so a service-reached mutator losing its ``note_commit`` would have gone
# unnoticed — and a failure after its commit would come back as a retryable
# « internal error » that writes twice.
_SERVICES = ATHENA / "services"
# Mutators that write NOTHING themselves: they delegate every write to the
# functions named here, which the sweep then holds to the rule instead.
_DELEGATING_MUTATORS: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    # One create_task per step, then the step↔task link.
    ("protocol", "create_linked_tasks"): (
        ("task", "create_task"), ("protocol", "_link_task_to_step"),
    ),
    # Detaching IS an update_hearing of the series fields.
    ("hearing", "unlink_hearing"): (("hearing", "update_hearing"),),
    # Finds, or creates through create_note.
    ("note", "ensure_analyse_note"): (("note", "create_note"),),
    # Lot 2A (T8). Finds the system folder, or adopts a legacy one, or
    # creates it — each in its own helper, which the sweep then holds.
    ("folder", "ensure_system_folder"): (
        ("folder", "_adopt_legacy"), ("folder", "_create_system_folder"),
    ),
    # A copy IS an ingestion of the source's object (a GCS rewrite).
    ("document", "copy_document"): (("document", "ingest_blob_as_document"),),
}
# Mutators that write THEMSELVES but stamp through ONE shared record
# builder: the stamp is checked on the builder, and the mutator must call it
# (the commit point stays the mutator's own, after its last write).
_STAMPED_THROUGH: dict[tuple[str, str], tuple[str, str]] = {
    ("document", "upload_document"): ("document", "_prepare_document_record"),
    ("document", "ingest_blob_as_document"): (
        "document", "_prepare_document_record"),
}
_STAMP_HELPERS = {"stamp_create", "stamp_update", "update_fields", "create_fields"}
# ``commit_document``/``commit_fields`` since 2026-09-25 (lot 0a, étape 5):
# the etag-guarded edits write through ``models.concurrency``, and its call
# IS the write — the ``set()``/``update()`` it performs lives in that module.
_WRITE_ATTRS = {"set", "update", "create", "commit",
                "commit_document", "commit_fields"}


def _model_references(path: pathlib.Path) -> set[tuple[str, str]]:
    """``(models module, function)`` for every ``<alias>.<verb>_…`` in the
    module at *path*, where ``<alias>`` is a ``from models import X as
    <alias>`` binding — and, since lot 2A (T8), every mutator imported BY
    NAME (``from models.X import verb_…``): services/gabarits reaches
    ``upload_document`` and ``ensure_system_folder`` that way, and an
    alias-only sweep never saw them."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {}
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module == "models" and node in tree.body:
            for a in node.names:
                aliases[a.asname or a.name] = a.name
        elif (node.module or "").startswith("models."):
            module = node.module.split(".", 1)[1]
            for a in node.names:
                if _MUTATOR_VERB.match(a.name):
                    found.add((module, a.name))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
                and _MUTATOR_VERB.match(node.attr)):
            found.add((aliases[node.value.id], node.attr))
    return found


def _services_reached() -> list[pathlib.Path]:
    """The ``services/*.py`` modules the handlers import (``from services
    import X [as Y]``) — derived, so a new service door is followed the day
    a handler starts using it."""
    tree = ast.parse(pathlib.Path(handlers.__file__).read_text(encoding="utf-8"))
    names = {a.name for node in ast.walk(tree)
             if isinstance(node, ast.ImportFrom) and node.module == "services"
             for a in node.names}
    return [_SERVICES / f"{n}.py" for n in sorted(names)]


def reached_mutators() -> set[tuple[str, str]]:
    """``(models module, function)`` for every mutator the connector's
    handlers reference — directly, or through a service module they
    import."""
    found = _model_references(pathlib.Path(handlers.__file__))
    for path in _services_reached():
        found |= _model_references(path)
    return found


def _function(module: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((MODELS / f"{module}.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(fn: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "provenance"]


def _in_except_handler(fn: ast.AST, target: ast.AST) -> bool:
    for handler in (n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)):
        if any(n is target for n in ast.walk(handler)):
            return True
    return False


def test_the_stamped_through_entries_are_reached_and_really_build_through():
    reached = reached_mutators()
    # Reached directly, or as the delegate of a reached delegating mutator
    # (the copy reaches ingest_blob_as_document that way).
    reached |= {d for key, delegates in _DELEGATING_MUTATORS.items()
                if key in reached for d in delegates}
    for (module, name), builder in _STAMPED_THROUGH.items():
        assert (module, name) in reached, f"stale entry: {module}.{name}"
        called = {n.func.id for n in ast.walk(_function(module, name))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert builder[1] in called, f"{module}.{name} no longer calls {builder[1]}"


def test_a_mutator_imported_by_name_is_reached(tmp_path):
    """Lot 2A (T8): the sweep follows ``from models.X import verb_…`` — the
    form services/gabarits uses — and not only the aliased modules."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from models.document import projet_document_name, upload_document\n"
        "from models.folder import SYSTEM_ROLE_PROJETS, ensure_system_folder\n"
        "def f():\n    upload_document()\n", encoding="utf-8")
    assert _model_references(probe) == {
        ("document", "upload_document"), ("folder", "ensure_system_folder")}


def test_the_generations_reach_the_document_creators():
    """fill_gabarit / create_document (lot 2A, T8) reach the two creators
    and the system folder through services/gabarits, and the copy through
    the model: every one of them is held to the commit-point rule."""
    assert "gabarits.py" in {p.name for p in _services_reached()}
    assert {("document", "upload_document"), ("folder", "ensure_system_folder"),
            ("document", "copy_document")} <= reached_mutators()


def test_the_reached_set_is_derived_and_not_vacuous():
    reached = reached_mutators()
    # Anchors, not an inventory: one per family the connector writes.
    assert {("task", "create_task"), ("partie", "update_partie"),
            ("time_entry", "set_time_entry_phase"),
            ("invoice", "create_invoice"),
            ("document", "record_analyse")} <= reached, reached
    # Non-mutators referenced from the handlers never qualify.
    assert not {n for _, n in reached} & {"display_name", "billing_address_from"}


def test_the_sweep_follows_the_service_doors_the_handlers_use():
    """The lot-1b writes the handlers reach ONLY through a service — never
    by a direct model reference — are in the swept set."""
    services = {p.name for p in _services_reached()}
    assert {"protocoles.py", "rendez_vous.py"} <= services, services
    direct = _model_references(pathlib.Path(handlers.__file__))
    via_services = reached_mutators() - direct
    assert {("protocol", "set_step_status"), ("protocol", "add_step"),
            ("hearing", "set_bookings_confirmation")} <= via_services, (
        via_services)


def test_the_delegating_mutators_are_reached_and_really_delegate():
    reached = reached_mutators()
    for (module, name), delegates in _DELEGATING_MUTATORS.items():
        assert (module, name) in reached, f"stale entry: {module}.{name}"
        fn = _function(module, name)
        called = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                  for n in ast.walk(fn) if isinstance(n, ast.Call)
                  and isinstance(n.func, (ast.Name, ast.Attribute))}
        for _, delegate in delegates:
            assert delegate in called, f"{module}.{name} no longer calls {delegate}"


def _assert_stamps_and_notes_its_commit(module: str, name: str) -> None:
    fn = _function(module, name)
    through = _STAMPED_THROUGH.get((module, name))
    if through is not None:
        called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name)}
        assert through[1] in called, (
            f"models.{module}.{name} no longer builds its record through "
            f"{through[1]}")
        builder = _function(*through)
        stamps = [c for h in _STAMP_HELPERS for c in _calls(builder, h)]
    else:
        stamps = [c for h in _STAMP_HELPERS for c in _calls(fn, h)]
    assert stamps, f"models.{module}.{name} writes without a provenance stamp"

    commits = [c for c in _calls(fn, "note_commit")
               if not _in_except_handler(fn, c)]
    assert commits, f"models.{module}.{name} never calls provenance.note_commit"
    writes = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Attribute) and n.func.attr in _WRITE_ATTRS)
        or (isinstance(n.func, ast.Name) and n.func.id.startswith("_txn"))
    )]
    assert writes and max(c.lineno for c in commits) > max(writes), (
        f"models.{module}.{name}: note_commit must follow the last write"
    )


@pytest.mark.parametrize(
    "module, name", sorted(reached_mutators()),
    ids=[f"{m}.{n}" for m, n in sorted(reached_mutators())],
)
def test_each_reached_mutator_stamps_and_notes_its_commit(module, name):
    for target in _DELEGATING_MUTATORS.get((module, name), ((module, name),)):
        _assert_stamps_and_notes_its_commit(*target)


# ══════════════════════════════════════════════════════════════════════
# 6. Le vrai magasin
# ══════════════════════════════════════════════════════════════════════


def _web_request():
    """A request on the real web blueprint's import name."""
    return _probe_app().test_request_context("/p_web")


def _dav_request():
    return _probe_app().test_request_context("/p_dav")


def test_a_task_created_by_the_connector_then_edited_on_the_phone(monkeypatch):
    fake = install(monkeypatch, task_model)
    with provenance.writing_via("mcp", tool="create_task"):
        task, errors = task_model.create_task({"title": "Préparer la requête"})
        assert provenance.committed_writes() == (("tasks", task["id"]),)
    assert errors == []
    stored = fake.peek(f"tasks/{task['id']}")
    assert stored["created_via"] == stored["updated_via"] == "mcp"
    first_mcp = stored["mcp_updated_at"]
    assert first_mcp is not None and stored["etag"] == task["etag"]

    with _dav_request():
        _, errors = task_model.update_task(task["id"], {"priority": "haute"})
    assert errors == []
    stored = fake.peek(f"tasks/{task['id']}")
    assert stored["updated_via"] == "dav"
    assert stored["created_via"] == "mcp"
    assert stored["mcp_updated_at"] == first_mcp  # sticky across the sync


def test_a_web_update_never_forges_or_erases_provenance(monkeypatch):
    fake = install(monkeypatch, time_entry_model)
    with _web_request():
        entry, errors = time_entry_model.create_time_entry({
            "dossier_id": "d1", "date": NOW, "description": "Recherche",
            "hours": 1.0, "rate": 30000, "billable": True,
            # A forged key in a web payload is not a VALID via: replaced.
            "created_via": "claude",
        })
    assert errors == []
    stored = fake.peek(f"timeentries/{entry['id']}")
    assert stored["created_via"] == stored["updated_via"] == "web"
    assert "mcp_updated_at" not in stored


def test_the_invoice_source_flips_carry_the_writer(monkeypatch):
    """create_invoice flips its sources INSIDE its transaction; the flip
    regenerates their etag, so it must say who flipped them. (An imported
    number from a past year keeps the year counter out of the test — it is
    never read on that path.)"""
    from models import expense as expense_model

    fake = install(monkeypatch, invoice_model, time_entry_model, expense_model)
    fake.seed("timeentries/te1", {
        "id": "te1", "dossier_id": "d1", "date": NOW, "description": "R",
        "hours": 1.0, "rate": 30000, "amount": 30000, "billable": True,
        "invoiced": False, "invoice_id": None, "etag": "e0",
    })
    with provenance.writing_via("mcp", tool="import_invoice"):
        invoice, errors = invoice_model.create_invoice(
            "d1", ["te1"], [],
            {"dossier_id": "d1", "client_id": "p1", "client_name": "Jean",
             "date": NOW},
            invoice_number="2025-F100",
        )
        commits = provenance.committed_writes()
    assert errors == [], errors
    assert commits == (("invoices", invoice["id"]),)
    flipped = fake.peek("timeentries/te1")
    assert flipped["invoiced"] is True
    assert flipped["updated_via"] == "mcp" and flipped["etag"] != "e0"
    assert flipped["mcp_updated_at"] is not None
    assert fake.peek(f"invoices/{invoice['id']}")["created_via"] == "mcp"


def test_create_then_append_note_emits_the_stored_etag(monkeypatch):
    """End to end through the real handler, run_write and the real note
    model: the write payload's etag IS the stored one, and the append
    reports the NEW etag, never the one it read.

    The append also leaves a revision since D17 (2026-09-27): its reference
    is built by ``models.revision``, which must share the fake store."""
    from models import revision as revision_model

    fake = install(monkeypatch, note_model, revision_model, dav_sync,
                   write_support)
    created = handlers.create_note({"title": "Recherche", "content": "Texte."})
    note = created["note"]
    stored = fake.peek(f"notes/{note['id']}")
    assert note["etag"] == stored["etag"]
    assert note["created_via"] == note["updated_via"] == "mcp"
    assert note["mcp_updated_at"] is not None
    assert stored["created_via"] == "mcp"

    appended = handlers.append_to_note(
        {"note_id": note["id"], "content": "Suite."})
    after = fake.peek(f"notes/{note['id']}")
    assert appended["note"]["etag"] == after["etag"] != stored["etag"]
    assert appended["note"]["created_via"] == "mcp"
    assert appended["note"]["updated_via"] == "mcp"
    (rev,) = fake.peek_collection(f"notes/{note['id']}/revisions").values()
    assert rev["previous_value"] == stored["content"] and rev["via"] == "mcp"
    assert rev["tool"] == "append_to_note"

    with _web_request():
        note_model.update_note(note["id"], {"pinned": True})
    web = fake.peek(f"notes/{note['id']}")
    assert web["updated_via"] == "web"
    assert web["mcp_updated_at"] == after["mcp_updated_at"]
    row = handlers.get_note({"note_id": note["id"]})["note"]
    assert row["etag"] == web["etag"]
    assert row["updated_via"] == "web" and row["created_via"] == "mcp"
    assert row["mcp_updated_at"] == appended["note"]["mcp_updated_at"]
