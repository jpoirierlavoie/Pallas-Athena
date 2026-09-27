"""Une facture qui se commet PENDANT l'édition ou la suppression d'une entrée
(plan, lot 0b, étape B2).

``update_time_entry`` / ``update_expense`` lisaient l'entrée, vérifiaient
qu'elle n'était pas facturée, puis réécrivaient le document ENTIER par un
``set()`` sans condition. Une facture créée entre les deux — son basculement
de source pose ``invoiced=True`` et ``invoice_id`` — voyait ces deux clés
réécrites à ``False``/``None`` par l'édition, alors que le poste de la
facture restait : le travail figurait sur la facture et paraissait non
facturé au registre, libre d'être facturé une seconde fois.

``delete_time_entry`` / ``delete_expense`` avaient la même forme : la
vérification, puis un ``delete()`` nu. Une facture commise entre les deux
laissait un poste citant une source supprimée.

Tout passe ici par les VRAIS modèles au-dessus du faux Firestore partagé
(le client, ses transactions et la boucle de reprise de ``transactional``
sont les vrais), et la facture rivale est la VRAIE ``create_invoice``,
lancée par un crochet de commit au moment exact où l'édition (ou la
suppression) envoie son écriture : après sa lecture, avant son application.
On relit ce qui est STOCKÉ.

Dans la section 1, les deux courses sans etag, les deux suppressions
courues, la lecture hors transaction et les clés forgées échouaient sur le
code d'avant (vérifié en revenant au modèle antérieur) ; la variante avec
etag, le refus d'une entrée déjà facturée, l'entrée absente et la
suppression qui survit à une écriture rivale NON facturante passaient déjà —
elles épinglent ce que le lot garde. La section 3 épingle ce que la page web
montre d'un refus de suppression, qui disparaissait jusque-là dans une
redirection muette ; ses deux derniers tests (revue du lot) visent la piste
de suppression et la double panne.
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
    from models import concurrency
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
DOSSIER = "d1"
WHEN = datetime(2026, 6, 15, tzinfo=UTC)
SEEDED = datetime(2026, 6, 16, 9, 0, tzinfo=UTC)
STALE = [concurrency.STALE_ETAG_ERROR]


def _fake_modules() -> list:
    """Every loaded module holding the Firestore client — derived, so a
    model the delete route starts to read tomorrow (the deletion journal
    already is one) cannot reach the mocked client instead."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    # Millésime FIGÉ : le numéro de la facture rivale lit today_mtl.
    monkeypatch.setattr(invoice_model, "today_mtl", lambda: date(2026, 6, 15))
    return fake


def _time_seed(**over) -> dict:
    doc = {
        "id": "e1", "dossier_id": DOSSIER, "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "date": WHEN,
        "description": "Rédaction de la défense", "phase": "", "sous_phase": "",
        "hours": 1.5, "rate": 30000, "amount": 45000, "billable": True,
        "invoiced": False, "invoice_id": None, "legacy_ref": "",
        "created_at": SEEDED, "created_via": "web", "updated_at": SEEDED,
        "updated_via": "web", "etag": "etag-0",
    }
    doc.update(over)
    return doc


def _expense_seed(**over) -> dict:
    doc = {
        "id": "x1", "dossier_id": DOSSIER, "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "date": WHEN,
        "description": "Timbre judiciaire", "category": "timbre_judiciaire",
        "phase": "", "sous_phase": "", "amount": 5000, "taxable": False,
        "receipt_document_id": None, "invoiced": False, "invoice_id": None,
        "legacy_ref": "", "created_at": SEEDED, "created_via": "web",
        "updated_at": SEEDED, "updated_via": "web", "etag": "etag-0",
    }
    doc.update(over)
    return doc


# Both models, one contract.
_KINDS = {
    "time_entry": {
        "path": "timeentries/e1", "row_id": "e1", "seed": _time_seed,
        "update": lambda: time_entry_model.update_time_entry,
        "delete": lambda: time_entry_model.delete_time_entry,
        "edit": {"description": "Rédaction de la défense (révisée)"},
        "invoice_args": lambda: (["e1"], []),
        "update_refused": "Impossible de modifier une entrée déjà facturée.",
        "delete_refused": "Impossible de supprimer une entrée déjà facturée.",
        "delete_url": "/temps/e1/delete",
        "route_getter": "get_time_entry",
        "delete_error": "Erreur lors de la suppression. Veuillez réessayer.",
    },
    "expense": {
        "path": "expenses/x1", "row_id": "x1", "seed": _expense_seed,
        "update": lambda: expense_model.update_expense,
        "delete": lambda: expense_model.delete_expense,
        "edit": {"description": "Timbre judiciaire (corrigé)"},
        "invoice_args": lambda: ([], ["x1"]),
        "update_refused": "Impossible de modifier une dépense déjà facturée.",
        "delete_refused": "Impossible de supprimer une dépense déjà facturée.",
        "delete_url": "/temps/depenses/x1/delete",
        "route_getter": "get_expense",
        "delete_error": "Erreur lors de la suppression. Veuillez réessayer.",
    },
}
_BOTH = pytest.mark.parametrize("kind", list(_KINDS), ids=list(_KINDS))


def _seed(db, kind, **over) -> dict:
    cfg = _KINDS[kind]
    db.seed(cfg["path"], cfg["seed"](**over))
    db.reset_logs()
    return cfg


def _invoice_during_next_write(db, kind) -> dict:
    """Arm a one-shot rival: the REAL ``create_invoice`` for this source,
    committed at the start of the next commit that writes the row — after
    the edit's (or the delete's) read, before its write applies.

    Returns a holder filled with the rival invoice once it has run.
    """
    cfg = _KINDS[kind]
    holder: dict = {}

    def _hook(info) -> None:
        if not any(path == cfg["path"] for _op, path in info.ops):
            return
        remove()  # one shot — the invoice's own commit must not re-enter
        entries, expenses = cfg["invoice_args"]()
        invoice, errors = invoice_model.create_invoice(
            DOSSIER, entries, expenses,
            {"dossier_id": DOSSIER, "date": WHEN},
        )
        assert errors == [], errors
        holder["invoice"] = invoice

    remove = db.add_commit_hook(_hook)
    return holder


def _line_items(db, invoice_id: str) -> dict:
    return db.peek_collection(f"invoices/{invoice_id}/lineitems")


# ══════════════════════════════════════════════════════════════════════
# 1. La facture rivale n'est jamais défaite
# ══════════════════════════════════════════════════════════════════════


@_BOTH
@pytest.mark.parametrize("with_etag", [False, True], ids=["sans-etag", "etag"])
def test_an_invoice_committing_during_an_edit_is_never_undone(
    db, kind, with_etag,
):
    """Sans etag (une page rendue avant que son formulaire n'en porte un, un
    script), l'ancien ``set()`` réécrivait ``invoiced=False`` par-dessus la
    facture. Avec etag, le basculement régénère l'etag : refus « périmé »."""
    cfg = _seed(db, kind)
    holder = _invoice_during_next_write(db, kind)

    kwargs = {"expected_etag": "etag-0"} if with_etag else {}
    doc, errors = cfg["update"]()(cfg["row_id"], dict(cfg["edit"]), **kwargs)

    invoice = holder["invoice"]  # the rival did run, mid-write
    assert doc is None
    assert errors == (STALE if with_etag else [cfg["update_refused"]])
    stored = db.peek(cfg["path"])
    assert stored["invoiced"] is True
    assert stored["invoice_id"] == invoice["id"]
    # The edit landed nowhere: the row is exactly what the invoice left.
    assert stored["description"] == cfg["seed"]()["description"]
    # And the invoice still cites a source that says it is invoiced.
    cited = {li["source_id"] for li in _line_items(db, invoice["id"]).values()}
    assert cited == {cfg["row_id"]}


@_BOTH
def test_an_invoice_committing_during_a_delete_leaves_the_entry(db, kind):
    """L'ancien ``delete()`` nu supprimait une source que la facture venait
    de citer : un poste orphelin, et un travail facturé dont la trace de
    temps avait disparu."""
    cfg = _seed(db, kind)
    holder = _invoice_during_next_write(db, kind)

    ok, error = cfg["delete"]()(cfg["row_id"])

    invoice = holder["invoice"]
    assert ok is False
    assert error == cfg["delete_refused"]
    stored = db.peek(cfg["path"])
    assert stored is not None
    assert stored["invoiced"] is True and stored["invoice_id"] == invoice["id"]
    cited = {li["source_id"] for li in _line_items(db, invoice["id"]).values()}
    assert cited == {cfg["row_id"]}


@_BOTH
def test_the_checks_read_the_entry_only_through_the_transaction(db, kind):
    """La lecture qui décide (facturée ? version ?) est celle de la
    transaction — une copie lue avant elle serait exactement la fenêtre que
    ce lot referme."""
    cfg = _seed(db, kind)

    doc, errors = cfg["update"]()(cfg["row_id"], dict(cfg["edit"]))
    assert errors == [], errors
    ok, error = cfg["delete"]()(cfg["row_id"])
    assert ok is True and error == ""

    outside = [r for r in db.reads_outside_transactions()
               if cfg["path"] in r.paths]
    assert outside == []
    guarded = [c for c in db.commits if c.transaction is not None]
    assert [op for c in guarded for op in c.ops] == [
        ("set", cfg["path"]), ("delete", cfg["path"])]
    assert all(c.transaction is not None for c in db.commits)


@_BOTH
def test_the_billing_link_and_identity_are_never_taken_from_the_caller(
    db, kind,
):
    """``invoiced``/``invoice_id`` appartiennent à la facturation seule ; un
    ``id`` transmis corromprait le CHAMP sans déplacer le document ; le
    tampon appartient à la provenance. L'ancien modèle fusionnait tout."""
    cfg = _seed(db, kind)
    forged = {
        **cfg["edit"],
        "invoiced": True, "invoice_id": "facture-forgee", "id": "autre-id",
        "created_at": datetime(2020, 1, 1, tzinfo=UTC), "created_via": "mcp",
        "etag": "etag-forge", "updated_via": "cron",
        "mcp_updated_at": datetime(2020, 1, 1, tzinfo=UTC),
    }

    doc, errors = cfg["update"]()(cfg["row_id"], forged)

    assert errors == [], errors
    stored = db.peek(cfg["path"])
    seed = cfg["seed"]()
    assert stored["description"] == cfg["edit"]["description"]
    assert stored["invoiced"] is False and stored["invoice_id"] is None
    assert stored["id"] == cfg["row_id"]
    assert stored["created_at"] == seed["created_at"]
    assert stored["created_via"] == "web"
    assert stored["etag"] not in ("etag-0", "etag-forge")
    assert stored["updated_via"] == "script"  # the path actually writing
    assert "mcp_updated_at" not in stored
    assert doc["etag"] == stored["etag"]


@_BOTH
def test_an_invoiced_entry_refuses_edit_and_delete_and_nothing_moves(db, kind):
    cfg = _seed(db, kind, invoiced=True, invoice_id="i1")
    before = db.peek(cfg["path"])

    doc, errors = cfg["update"]()(cfg["row_id"], dict(cfg["edit"]))
    assert doc is None and errors == [cfg["update_refused"]]
    ok, error = cfg["delete"]()(cfg["row_id"])
    assert ok is False and error == cfg["delete_refused"]

    assert db.peek(cfg["path"]) == before
    assert db.commits == []


@_BOTH
def test_a_missing_entry_is_refused_by_both(db, kind):
    cfg = _KINDS[kind]

    doc, errors = cfg["update"]()(cfg["row_id"], dict(cfg["edit"]))
    assert doc is None and len(errors) == 1 and "introuvable" in errors[0]
    ok, error = cfg["delete"]()(cfg["row_id"])
    assert ok is False and "introuvable" in error
    assert db.peek(cfg["path"]) is None and db.commits == []


@_BOTH
def test_a_delete_racing_a_rival_edit_rereads_and_still_deletes(db, kind):
    """Une écriture rivale qui ne facture PAS (le téléphone, un autre
    onglet) fait avorter la transaction ; la reprise relit l'entrée, qui
    n'est toujours pas facturée, et la supprime."""
    cfg = _seed(db, kind)

    def _hook(info) -> None:
        if ("delete", cfg["path"]) in info.ops:
            remove()
            db.external_write(cfg["path"], {**db.peek(cfg["path"]),
                                            "description": "Rival",
                                            "etag": "etag-rival"})

    remove = db.add_commit_hook(_hook)
    ok, error = cfg["delete"]()(cfg["row_id"])

    assert ok is True and error == ""
    assert db.peek(cfg["path"]) is None


# ══════════════════════════════════════════════════════════════════════
# 2. Le reclassement de phase garde sa forme
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind, setter", [
    ("time_entry", lambda: time_entry_model.set_time_entry_phase),
    ("expense", lambda: expense_model.set_expense_phase),
], ids=["time_entry", "expense"])
def test_a_billed_row_still_reclassifies_by_a_partial_update(db, kind, setter):
    """Le mur ``invoiced`` que ce lot rend étanche n'est PAS celui du
    reclassement : sa garantie est la forme de son écriture, un ``update()``
    partiel de la paire et de son tampon."""
    cfg = _seed(db, kind, invoiced=True, invoice_id="i1")
    before = db.peek(cfg["path"])

    doc, errors, changed = setter()(cfg["row_id"], "", "INT-01")

    assert errors == [] and changed is True
    assert [op for c in db.commits for op in c.ops] == [("update", cfg["path"])]
    after = db.peek(cfg["path"])
    moved = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert moved <= {"phase", "sous_phase", "updated_at", "etag", "updated_via"}
    assert after["invoiced"] is True and after["invoice_id"] == "i1"


# ══════════════════════════════════════════════════════════════════════
# 3. La page web dit pourquoi une suppression est refusée
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


@_BOTH
def test_a_refused_delete_re_renders_the_form_with_the_reason(
    db, client, kind,
):
    """La page affichait encore « Supprimer » (une facture a été émise dans
    un autre onglet) : le refus revenait en redirection muette vers la liste,
    et l'entrée y figurait toujours sans un mot. Il revient maintenant en 200,
    sur le formulaire de l'entrée telle qu'elle est, avec la raison."""
    cfg = _seed(db, kind, invoiced=True, invoice_id="i1")

    resp = client.post(cfg["delete_url"], data={"csrf_token": "tok"})

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert cfg["delete_refused"] in body
    # The form shows the row AS STORED: its invoiced banner, no delete button.
    assert "liée à une facture" in body
    assert cfg["delete_url"] not in body
    assert db.peek(cfg["path"])["invoiced"] is True
    assert db.peek_collection("audit_events") == {}


@_BOTH
def test_a_delete_refused_by_a_mid_request_invoice_says_so(db, client, kind):
    cfg = _seed(db, kind)
    holder = _invoice_during_next_write(db, kind)

    resp = client.post(cfg["delete_url"], data={"csrf_token": "tok"})

    assert holder["invoice"]
    assert resp.status_code == 200
    assert cfg["delete_refused"] in resp.get_data(as_text=True)
    assert db.peek(cfg["path"]) is not None
    assert db.peek_collection("audit_events") == {}


@_BOTH
def test_a_successful_delete_still_redirects_and_is_journaled(db, client, kind):
    cfg = _seed(db, kind)

    resp = client.post(cfg["delete_url"], data={"csrf_token": "tok"})

    assert resp.status_code == 302
    assert db.peek(cfg["path"]) is None
    events = list(db.peek_collection("audit_events").values())
    assert len(events) == 1 and events[0]["entity_id"] == cfg["row_id"]


@_BOTH
def test_deleting_an_entry_already_gone_redirects(db, client, kind):
    """Rien à expliquer : ce que l'utilisateur voulait — que l'entrée
    disparaisse — est déjà vrai, et la liste le montre."""
    cfg = _KINDS[kind]

    resp = client.post(cfg["delete_url"], data={"csrf_token": "tok"})

    assert resp.status_code == 302
    assert db.peek_collection("audit_events") == {}


@_BOTH
def test_a_delete_after_a_void_journals_no_invoiced_status(
    db, client, monkeypatch, kind,
):
    """La piste de suppression portait « facturée » lu de la PRÉ-LECTURE de
    la route. La suppression, elle, ne passe que si la transaction relit
    l'entrée NON facturée : une facture annulée entre les deux faisait
    inscrire au journal une entrée « facturée »… supprimée précisément parce
    qu'elle ne l'était plus."""
    cfg = _seed(db, kind, invoiced=True, invoice_id="i1")
    real_get = getattr(time_expenses_routes, cfg["route_getter"])
    calls: list = []

    def pre_read_then_void(row_id):
        doc = real_get(row_id)
        if not calls:
            # The void lands right after the route's pre-read.
            db.external_write(cfg["path"], {
                **db.peek(cfg["path"]), "invoiced": False,
                "invoice_id": None, "etag": "etag-void",
            })
        calls.append(row_id)
        return doc

    monkeypatch.setattr(time_expenses_routes, cfg["route_getter"],
                        pre_read_then_void)

    resp = client.post(cfg["delete_url"], data={"csrf_token": "tok"})

    assert resp.status_code == 302
    assert db.peek(cfg["path"]) is None
    events = list(db.peek_collection("audit_events").values())
    assert len(events) == 1
    assert events[0]["snapshot_min"]["status"] == ""


@_BOTH
def test_a_failed_delete_whose_reread_also_fails_still_says_why(
    db, client, monkeypatch, kind,
):
    """Le magasin refuse la suppression, puis la relecture de la route
    échoue aussi (elle se replie sur None) : la route redirigeait alors vers
    la liste sans un mot. L'entrée n'est PAS connue disparue — la raison se
    dit, sur l'entrée telle qu'elle était avant la requête."""
    cfg = _seed(db, kind)

    def _outage(info) -> None:
        if any(path == cfg["path"] for _op, path in info.ops):
            remove()
            raise RuntimeError("panne simulée du magasin")

    remove = db.add_commit_hook(_outage)
    real_get = getattr(time_expenses_routes, cfg["route_getter"])
    calls: list = []

    def reread_fails(row_id):
        calls.append(row_id)
        return real_get(row_id) if len(calls) == 1 else None

    monkeypatch.setattr(time_expenses_routes, cfg["route_getter"],
                        reread_fails)

    resp = client.post(cfg["delete_url"], data={"csrf_token": "tok"})

    assert len(calls) == 2  # the pre-read, then the failed re-read
    assert resp.status_code == 200
    assert cfg["delete_error"] in resp.get_data(as_text=True)
    assert db.peek(cfg["path"]) is not None
    assert db.peek_collection("audit_events") == {}
