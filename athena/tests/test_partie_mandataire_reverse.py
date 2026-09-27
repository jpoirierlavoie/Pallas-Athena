"""Mandataires : la règle À REBOURS (lot 0b).

``_validate`` ne vérifiait que la règle DIRECTE — chacun de MES mandataires
doit être une personne physique de MON rôle. Rien ne vérifiait l'inverse :
changer le rôle ou le type d'un contact qu'un AUTRE contact désigne comme
mandataire laissait ce dernier en violation de la règle directe, si bien
que toute édition ultérieure du contact REPRÉSENTÉ était refusée — au
formulaire web comme par un PUT CardDAV (422, que DavX5 avale) — sans que
rien ne désigne la cause.

La réparation vit au modèle (``update_partie``), donc le formulaire web, le
PUT CardDAV et le connecteur passent tous par elle. Ces tests passent par
le VRAI client Firestore (``tests/_fake_firestore.py`` : seul le serveur est
faux), les vraies routes et les vrais gabarits ; on relit ce qui est
STOCKÉ.
"""

import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.carddav as carddav
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import partie as pm
    import routes.parties as parties_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _personne(pid: str, first: str, last: str, role: str = "client",
              **over) -> dict:
    doc = pm._default_doc()
    doc.update({
        "id": pid, "type": "individual", "contact_role": role,
        "first_name": first, "last_name": last,
        "etag": f"etag-{pid}", "vcard_uid": f"uid-{pid}",
    })
    doc.update(over)
    return doc


@pytest.fixture
def famille(db):
    """Sophie (cliente) est représentée par sa tutrice Marie (cliente)."""
    db.seed("parties/marie", _personne("marie", "Marie", "Gagnon"))
    db.seed("parties/sophie", _personne(
        "sophie", "Sophie", "Gagnon",
        mandataires=[{"id": "marie", "kind": "tuteur", "notes": ""}],
    ))
    return db


# ══════════════════════════════════════════════════════════════════════
# Le modèle
# ══════════════════════════════════════════════════════════════════════


def test_a_role_change_on_a_mandataire_is_refused(famille):
    """THE defect: on the old code this write committed, and Sophie became
    uneditable (« Mandataire « Marie Gagnon » : doit avoir le même rôle… »)."""
    before = famille.peek("parties/marie")
    doc, errors = pm.update_partie("marie", {"contact_role": "témoin"})
    assert doc is None
    assert len(errors) == 1
    assert "mandataire de Sophie Gagnon" in errors[0]
    assert famille.peek("parties/marie") == before


def test_the_represented_contact_stays_editable(famille):
    """The symptom the guard exists for: after a refused change, the
    REPRESENTED contact still saves."""
    pm.update_partie("marie", {"contact_role": "témoin"})
    _, errors = pm.update_partie("sophie", {"email": "sophie@exemple.ca"})
    assert errors == []
    assert famille.peek("parties/sophie")["email"] == "sophie@exemple.ca"


def test_a_type_change_to_organization_is_refused(famille):
    doc, errors = pm.update_partie(
        "marie", {"type": "organization", "organization_name": "Gagnon inc."}
    )
    assert doc is None and "personne physique" in errors[0]
    assert famille.peek("parties/marie")["type"] == "individual"


def test_a_change_that_repairs_a_legacy_mismatch_is_allowed(db):
    """Never lock out a REPAIR: the referrer is a client, the mandataire a
    witness (a pre-guard mismatch that already bricks the referrer).
    Moving the mandataire to « client » fixes it and must commit."""
    db.seed("parties/marie", _personne("marie", "Marie", "Gagnon",
                                       role="témoin"))
    db.seed("parties/sophie", _personne(
        "sophie", "Sophie", "Gagnon",
        mandataires=[{"id": "marie", "kind": "tuteur", "notes": ""}],
    ))
    _, errors = pm.update_partie("marie", {"contact_role": "client"})
    assert errors == []
    assert db.peek("parties/marie")["contact_role"] == "client"
    _, errors = pm.update_partie("sophie", {"notes": "réparée"})
    assert errors == []


def test_a_contact_nobody_lists_changes_role_freely(famille):
    _, errors = pm.update_partie("sophie", {"contact_role": "témoin",
                                            "mandataires": []})
    assert errors == []


def test_no_scan_unless_the_role_or_type_changes(famille, monkeypatch):
    """The collection scan is paid ONLY on an actual role/type change."""
    def _boom(pid):
        raise AssertionError("scan de la collection inattendu")

    monkeypatch.setattr(pm, "list_mandataire_referrers", _boom)
    _, errors = pm.update_partie("marie", {"email": "marie@exemple.ca",
                                           "contact_role": "client",
                                           "type": "individual"})
    assert errors == []


def test_an_unverifiable_check_refuses(famille, monkeypatch):
    def _outage(pid):
        raise RuntimeError("lecture impossible")

    monkeypatch.setattr(pm, "list_mandataire_referrers", _outage)
    before = famille.peek("parties/marie")
    doc, errors = pm.update_partie("marie", {"contact_role": "témoin"})
    assert doc is None and errors == [pm.MANDATAIRE_CHECK_UNAVAILABLE]
    assert famille.peek("parties/marie") == before


def test_list_mandataire_referrers_reads_both_shapes(db):
    db.seed("parties/marie", _personne("marie", "Marie", "Gagnon"))
    db.seed("parties/sophie", _personne(
        "sophie", "Sophie", "Gagnon",
        mandataires=[{"id": "marie", "kind": "tuteur", "notes": ""}]))
    # A not-yet-migrated document carrying the legacy single field.
    db.seed("parties/ancien", _personne(
        "ancien", "Luc", "Gagnon", mandataire_id="marie"))
    db.seed("parties/autre", _personne("autre", "Paul", "Roy"))
    ids = sorted(r["id"] for r in pm.list_mandataire_referrers("marie"))
    assert ids == ["ancien", "sophie"]


def test_list_mandataire_referrers_propagates_and_refuses_an_empty_id(
    db, monkeypatch
):
    with pytest.raises(ValueError):
        pm.list_mandataire_referrers("")

    class _Broken:
        def stream(self):
            raise RuntimeError("panne")

    monkeypatch.setattr(pm, "db", mock.Mock(collection=lambda n: _Broken()))
    with pytest.raises(RuntimeError):
        pm.list_mandataire_referrers("marie")


# ══════════════════════════════════════════════════════════════════════
# Le formulaire web
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def web(db):
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
    app.register_blueprint(parties_routes.parties_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def test_the_web_form_shows_the_refusal_and_writes_nothing(famille, web):
    """A 200 re-render carrying the French reason — never a silent refusal."""
    before = famille.peek("parties/marie")
    resp = web.post("/parties/marie", data={
        "type": "individual", "contact_role": "témoin",
        "first_name": "Marie", "last_name": "Gagnon",
        "expected_etag": before["etag"],
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Ce contact est mandataire de Sophie Gagnon" in body
    assert famille.peek("parties/marie") == before


# ══════════════════════════════════════════════════════════════════════
# CardDAV
# ══════════════════════════════════════════════════════════════════════


def _vcard(role_label: str) -> str:
    return "\r\n".join([
        "BEGIN:VCARD", "VERSION:4.0", "UID:uid-marie",
        "FN:Marie Gagnon", "N:Gagnon;Marie;;;",
        f"CATEGORIES:{role_label}", "END:VCARD", "",
    ])


@pytest.fixture
def dav(db, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


def test_a_carddav_put_changing_a_mandataire_role_is_a_french_422(
    famille, dav, caplog
):
    before = famille.peek("parties/marie")
    with caplog.at_level(logging.WARNING, logger=carddav.logger.name):
        resp = dav.put("/dav/addressbook/marie.vcf",
                       data=_vcard(pm.ROLE_LABELS["témoin"]).encode("utf-8"),
                       headers={**AUTH, "Content-Type": "text/vcard"})
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert body.startswith("Données invalides : ")
    assert "mandataire de Sophie Gagnon" in body
    assert famille.peek("parties/marie") == before
    # The log line carries a count, never the contact's name (the
    # redaction filter does not scrub names).
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Sophie" not in logged and "Gagnon" not in logged


def test_a_carddav_put_whose_check_cannot_run_is_a_503(
    famille, dav, monkeypatch
):
    def _outage(pid):
        raise RuntimeError("lecture impossible")

    monkeypatch.setattr(pm, "list_mandataire_referrers", _outage)
    resp = dav.put("/dav/addressbook/marie.vcf",
                   data=_vcard(pm.ROLE_LABELS["témoin"]).encode("utf-8"),
                   headers={**AUTH, "Content-Type": "text/vcard"})
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After")


# ══════════════════════════════════════════════════════════════════════
# Le connecteur
# ══════════════════════════════════════════════════════════════════════


def test_the_connector_meets_the_same_rule(famille):
    """update_partie over MCP can change contact_role: it reaches the model
    guard and surfaces its French reason, writing nothing."""
    assert write_support.db is famille  # the claim store is the fake too
    before = famille.peek("parties/marie")
    with pytest.raises(tools.ToolArgumentError, match="mandataire de Sophie"):
        handlers.update_partie({"partie_id": "marie",
                                "contact_role": "témoin"})
    assert famille.peek("parties/marie") == before
