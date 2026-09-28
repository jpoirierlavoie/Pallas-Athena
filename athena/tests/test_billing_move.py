"""Déplacer une entrée de temps ou un déboursé NON FACTURÉ vers un autre
dossier — UNE règle (plan, lot 3a, étape 2).

``move_time_entry`` / ``move_expense`` (le déplacement que l'outil du
connecteur atteindra au lot 3b) et les formulaires d'édition web, dont le
sélecteur de dossier a toujours refiché une ligne, passent désormais par la
MÊME règle (``models/billing_move``) :

* une ligne facturée ne se déplace jamais ;
* le dossier cible est relu DANS la transaction de l'écrivain, et les deux
  instantanés d'affichage (numéro, intitulé) viennent de CETTE lecture —
  jamais de l'appelant ;
* la phase reste : les phases sont orthogonales au dossier ;
* ``move_*`` n'écrit qu'un ``update()`` partiel des trois clés du dossier et
  du tampon de provenance — heures, taux, montant, description hors
  d'atteinte par la FORME de l'écriture.

Tout passe par les VRAIS modèles au-dessus du faux Firestore partagé (le
client, ses transactions et la boucle de reprise de ``transactional`` sont
les vrais) ; on relit ce qui est STOCKÉ. Deux tests échouaient sur le code
d'avant (vérifié en revenant au modèle antérieur) : l'édition web vers un
dossier disparu enregistrait l'entrée sous un identifiant mort, et
l'instantané d'étiquettes venait de l'appelant (un intitulé périmé survivait
au déplacement). Les autres épinglent le contrat du nouvel écrivain.
"""

import os
import pathlib
import sys
from datetime import date, datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import billing_move, concurrency, provenance
    from models import expense as expense_model
    from models import invoice as invoice_model
    from models import time_entry as time_entry_model
    import routes.time_expenses as time_expenses_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 6, 15, tzinfo=UTC)
SEEDED = datetime(2026, 6, 16, 9, 0, tzinfo=UTC)
STALE = [concurrency.STALE_ETAG_ERROR]
# What a move may change on the stored row — the dossier link, its two
# snapshots, and the write's own stamp. Nothing else.
MOVE_KEYS = {
    "dossier_id", "dossier_file_number", "dossier_title",
    "updated_at", "etag", "updated_via",
}


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    monkeypatch.setattr(invoice_model, "today_mtl", lambda: date(2026, 6, 15))
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "Tremblay c. Lavoie"})
    fake.seed("dossiers/d2", {"id": "d2", "file_number": "2026-002",
                              "title": "Gagnon c. Roy"})
    return fake


def _time_seed(**over) -> dict:
    doc = {
        "id": "e1", "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "date": WHEN,
        "description": "Rédaction de la défense", "phase": "CTS",
        "sous_phase": "CTS-02", "hours": 1.5, "rate": 30000,
        "amount": 45000, "billable": True, "invoiced": False,
        "invoice_id": None, "legacy_ref": "", "created_at": SEEDED,
        "created_via": "web", "updated_at": SEEDED, "updated_via": "web",
        "etag": "etag-0",
    }
    doc.update(over)
    return doc


def _expense_seed(**over) -> dict:
    doc = {
        "id": "x1", "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "date": WHEN,
        "description": "Timbre judiciaire", "category": "timbre_judiciaire",
        "phase": "CTS", "sous_phase": "CTS-02", "amount": 5000,
        "taxable": False, "receipt_document_id": None, "invoiced": False,
        "invoice_id": None, "legacy_ref": "", "created_at": SEEDED,
        "created_via": "web", "updated_at": SEEDED, "updated_via": "web",
        "etag": "etag-0",
    }
    doc.update(over)
    return doc


_KINDS = {
    "time_entry": {
        "path": "timeentries/e1", "row_id": "e1", "seed": _time_seed,
        "move": lambda: time_entry_model.move_time_entry,
        "update": lambda: time_entry_model.update_time_entry,
        "invoiced": time_entry_model.MOVE_INVOICED_ERROR,
        "missing": "Entrée de temps introuvable.",
        "invoice_args": lambda: (["e1"], []),
        "edit_url": "/temps/e1",
    },
    "expense": {
        "path": "expenses/x1", "row_id": "x1", "seed": _expense_seed,
        "move": lambda: expense_model.move_expense,
        "update": lambda: expense_model.update_expense,
        "invoiced": expense_model.MOVE_INVOICED_ERROR,
        "missing": "Dépense introuvable.",
        "invoice_args": lambda: ([], ["x1"]),
        "edit_url": "/temps/depenses/x1",
    },
}
_BOTH = pytest.mark.parametrize("kind", list(_KINDS), ids=list(_KINDS))


def _seed(db, kind, **over) -> dict:
    cfg = _KINDS[kind]
    db.seed(cfg["path"], cfg["seed"](**over))
    db.reset_logs()
    return cfg


def _changed(before: dict, after: dict) -> set:
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}


# ══════════════════════════════════════════════════════════════════════
# 1. move_* — la forme de l'écriture, et l'ordre des contrôles
# ══════════════════════════════════════════════════════════════════════


@_BOTH
def test_a_move_writes_only_the_dossier_link_and_its_stamp(db, kind):
    cfg = _seed(db, kind)
    before = db.peek(cfg["path"])

    doc, errors, moved = cfg["move"]()(
        cfg["row_id"], {"id": "d2"}, from_dossier_id="d1",
        expected_etag="etag-0",
    )

    assert errors == [] and moved is True
    # ONE partial update — never the merged full-document set().
    assert [op for c in db.commits for op in c.ops] == [("update", cfg["path"])]
    after = db.peek(cfg["path"])
    assert _changed(before, after) == MOVE_KEYS
    assert (after["dossier_id"], after["dossier_file_number"],
            after["dossier_title"]) == ("d2", "2026-002", "Gagnon c. Roy")
    # The phase stays: phases are orthogonal to the dossier.
    assert (after["phase"], after["sous_phase"]) == ("CTS", "CTS-02")
    assert after["etag"] != "etag-0"
    # The returned doc is the stored one, new etag included.
    assert doc["etag"] == after["etag"] and doc["dossier_id"] == "d2"


@_BOTH
def test_the_labels_come_from_the_target_read_inside_the_transaction(db, kind):
    """Only the caller's ``id`` is trusted: its labels are re-read, through
    the move's own transaction."""
    cfg = _seed(db, kind)

    doc, errors, moved = cfg["move"]()(
        cfg["row_id"],
        {"id": "d2", "file_number": "FORGÉ", "title": "Intitulé périmé"},
        from_dossier_id="d1",
    )

    assert errors == [] and moved
    stored = db.peek(cfg["path"])
    assert stored["dossier_file_number"] == "2026-002"
    assert stored["dossier_title"] == "Gagnon c. Roy"
    target_reads = [r for r in db.reads if "dossiers/d2" in r.paths]
    assert target_reads and all(r.transactional for r in target_reads)
    assert [r for r in db.reads_outside_transactions()
            if cfg["path"] in r.paths] == []


@_BOTH
def test_an_invoiced_row_never_moves(db, kind):
    cfg = _seed(db, kind, invoiced=True, invoice_id="i1")

    doc, errors, moved = cfg["move"]()(
        cfg["row_id"], {"id": "d2"}, from_dossier_id="d1",
    )

    assert doc is None and moved is False
    assert errors == [cfg["invoiced"]]
    assert "annulez d'abord la facture" in errors[0]
    assert db.commits == []


@_BOTH
def test_a_from_dossier_that_is_not_the_stored_one_is_refused_by_name(db, kind):
    """The optimistic confirmation of the caller's view — independent of
    the etag: the refusal names the dossier the row IS in (its file number,
    never its title)."""
    cfg = _seed(db, kind)

    doc, errors, moved = cfg["move"]()(
        cfg["row_id"], {"id": "d2"}, from_dossier_id="d9",
    )

    assert doc is None and moved is False
    assert errors == [billing_move.from_mismatch_error(cfg["seed"]())]
    assert "« 2026-001 »" in errors[0]
    assert "Tremblay" not in errors[0]
    assert db.commits == []


@_BOTH
def test_a_row_already_in_the_target_is_a_no_op_whatever_else_is_said(db, kind):
    """Checked BEFORE the « from » and the etag, so a replay after success
    writes nothing and reports nothing false."""
    cfg = _seed(db, kind, dossier_id="d2", dossier_file_number="2026-002")

    doc, errors, moved = cfg["move"]()(
        cfg["row_id"], {"id": "d2"}, from_dossier_id="d1",
        expected_etag="périmé",
    )

    assert errors == [] and moved is False
    assert doc["etag"] == "etag-0"
    # The transaction commits EMPTY — no write of any kind.
    assert [op for c in db.commits for op in c.ops] == []


@_BOTH
def test_a_stale_etag_moves_nothing(db, kind):
    cfg = _seed(db, kind)

    doc, errors, moved = cfg["move"]()(
        cfg["row_id"], {"id": "d2"}, from_dossier_id="d1",
        expected_etag="etag-autre",
    )

    assert (doc, errors, moved) == (None, STALE, False)
    assert db.peek(cfg["path"])["dossier_id"] == "d1"
    assert db.commits == []


@_BOTH
def test_a_missing_target_or_row_is_refused(db, kind):
    cfg = _seed(db, kind)
    move = cfg["move"]()

    assert move(cfg["row_id"], {"id": "d404"}, from_dossier_id="d1") == (
        None, [billing_move.TARGET_NOT_FOUND], False)
    assert move(cfg["row_id"], {"id": ""}, from_dossier_id="d1") == (
        None, [billing_move.TARGET_REQUIRED], False)
    assert move(cfg["row_id"], {"id": "d2/x/y"}, from_dossier_id="d1") == (
        None, [billing_move.TARGET_REQUIRED], False)
    assert move("absent", {"id": "d2"}, from_dossier_id="d1") == (
        None, [cfg["missing"]], False)
    # A slashed row id addresses nothing — never a deeper record.
    assert move("e1/a/b", {"id": "d2"}, from_dossier_id="d1") == (
        None, [cfg["missing"]], False)
    assert db.commits == []


@_BOTH
def test_an_invoice_committing_during_a_move_wins_and_the_move_refuses(db, kind):
    """The invoiced check reads the TRANSACTION's snapshot: an invoice
    committed between the move's read and its commit aborts the move, and
    the retry re-reads the row as invoiced."""
    cfg = _seed(db, kind)
    db.seed("parties/p1", {"id": "p1", "type": "individual",
                           "first_name": "Jean", "last_name": "Tremblay"})
    holder: dict = {}

    def _hook(info) -> None:
        if ("update", cfg["path"]) not in info.ops:
            return
        remove()
        entries, expenses = cfg["invoice_args"]()
        invoice, errors = invoice_model.create_invoice(
            "d1", entries, expenses,
            {"dossier_id": "d1", "date": WHEN, "client_id": "p1",
             "client_name": "Jean Tremblay",
             "billing_address": {"name": "Jean Tremblay"},
             "gst_number": "123456789 RT0001",
             "qst_number": "1234567890 TQ0001"},
        )
        assert errors == [], errors
        holder["invoice"] = invoice

    remove = db.add_commit_hook(_hook)

    doc, errors, moved = cfg["move"]()(
        cfg["row_id"], {"id": "d2"}, from_dossier_id="d1",
    )

    assert holder["invoice"]  # the rival did run, mid-move
    assert (doc, moved) == (None, False)
    assert errors == [cfg["invoiced"]]
    stored = db.peek(cfg["path"])
    assert stored["dossier_id"] == "d1"
    assert stored["invoiced"] is True


@_BOTH
def test_a_target_deleted_during_the_move_is_refused_on_the_retry(db, kind):
    cfg = _seed(db, kind)

    def _hook(info) -> None:
        if ("update", cfg["path"]) in info.ops:
            remove()
            db.external_delete("dossiers/d2")

    remove = db.add_commit_hook(_hook)

    doc, errors, moved = cfg["move"]()(
        cfg["row_id"], {"id": "d2"}, from_dossier_id="d1",
    )

    assert (doc, errors, moved) == (None, [billing_move.TARGET_NOT_FOUND], False)
    assert db.peek(cfg["path"])["dossier_id"] == "d1"


@_BOTH
def test_a_connector_move_is_stamped_and_noted_as_committed(db, kind):
    cfg = _seed(db, kind)

    with provenance.writing_via("mcp", tool="move_test"):
        _, errors, moved = cfg["move"]()(
            cfg["row_id"], {"id": "d2"}, from_dossier_id="d1",
        )
        assert errors == [] and moved
        noted = provenance.committed_writes()

    stored = db.peek(cfg["path"])
    assert stored["updated_via"] == "mcp"
    assert stored["mcp_updated_at"] is not None
    assert noted == ((cfg["path"].split("/")[0], cfg["row_id"]),)

    # A replay writes nothing — and notes nothing a retry would repeat.
    with provenance.writing_via("mcp", tool="move_test"):
        _, errors, moved = cfg["move"]()(
            cfg["row_id"], {"id": "d2"}, from_dossier_id="d1",
        )
        assert errors == [] and moved is False
        assert provenance.committed_writes() == ()


# ══════════════════════════════════════════════════════════════════════
# 2. L'édition complète suit la MÊME règle quand le dossier change
# ══════════════════════════════════════════════════════════════════════


@_BOTH
def test_an_edit_that_changes_the_dossier_takes_the_target_labels(db, kind):
    """Régression — le modèle d'avant écrivait les étiquettes que l'appelant
    avait résolues hors de toute transaction : un intitulé périmé (ou forgé)
    survivait au déplacement."""
    cfg = _seed(db, kind)

    doc, errors = cfg["update"]()(
        cfg["row_id"],
        {"dossier_id": "d2", "dossier_file_number": "PÉRIMÉ",
         "dossier_title": "Ancien intitulé"},
        expected_etag="etag-0",
    )

    assert errors == [], errors
    stored = db.peek(cfg["path"])
    assert (stored["dossier_id"], stored["dossier_file_number"],
            stored["dossier_title"]) == ("d2", "2026-002", "Gagnon c. Roy")
    assert (stored["phase"], stored["sous_phase"]) == ("CTS", "CTS-02")
    target_reads = [r for r in db.reads if "dossiers/d2" in r.paths]
    assert target_reads and all(r.transactional for r in target_reads)


@_BOTH
def test_an_edit_towards_a_vanished_dossier_is_refused(db, kind):
    """Régression — l'édition enregistrait l'entrée sous l'identifiant d'un
    dossier qui n'existait plus (les étiquettes de l'appelant par-dessus)."""
    cfg = _seed(db, kind)

    doc, errors = cfg["update"]()(
        cfg["row_id"],
        {"dossier_id": "d404", "dossier_file_number": "2026-404",
         "dossier_title": "Disparu"},
    )

    assert doc is None
    assert errors == [billing_move.TARGET_NOT_FOUND]
    assert db.peek(cfg["path"])["dossier_id"] == "d1"
    assert db.commits == []


@_BOTH
def test_an_edit_that_keeps_the_dossier_reads_no_dossier(db, kind):
    cfg = _seed(db, kind)

    doc, errors = cfg["update"]()(
        cfg["row_id"], {"dossier_id": "d1", "description": "Révisée"},
    )

    assert errors == [], errors
    assert [r for r in db.reads if any(p.startswith("dossiers/") for p in r.paths)] == []


# ══════════════════════════════════════════════════════════════════════
# 3. Le formulaire web : le refichage passe par la règle
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(db):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    app.register_blueprint(time_expenses_routes.time_expenses_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _form(kind: str, **over) -> dict:
    base = {"csrf_token": "tok", "expected_etag": "etag-0",
            "dossier_id": "d2", "date": "2026-06-15",
            "description": "Rédaction de la défense",
            "phase": "CTS", "sous_phase": "CTS-02"}
    if kind == "time_entry":
        base.update(hours="1.5", rate="300.00", billable="on")
    else:
        base.update(amount="50.00", category="timbre_judiciaire")
    base.update(over)
    return base


@_BOTH
def test_the_edit_form_moves_a_row_by_the_rule(db, client, kind):
    cfg = _seed(db, kind)

    resp = client.post(cfg["edit_url"], data=_form(kind))

    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    stored = db.peek(cfg["path"])
    assert (stored["dossier_id"], stored["dossier_file_number"]) == (
        "d2", "2026-002")


@_BOTH
def test_the_edit_form_says_why_a_vanished_target_is_refused(
    db, client, kind, monkeypatch,
):
    """The page resolved the dossier (it existed when the route looked);
    the model's transaction no longer finds it. The refusal is SAID, at
    200, on the form — nothing moved."""
    cfg = _seed(db, kind)
    monkeypatch.setattr(
        time_expenses_routes, "get_dossier",
        lambda did: {"id": did, "file_number": "2026-404", "title": "Disparu"},
    )

    resp = client.post(cfg["edit_url"], data=_form(kind, dossier_id="d404"))

    assert resp.status_code == 200
    assert "Le dossier de destination est introuvable" in resp.get_data(as_text=True)
    assert db.peek(cfg["path"])["dossier_id"] == "d1"
