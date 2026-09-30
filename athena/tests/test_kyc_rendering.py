"""La fiche d'un contact dit QUI a décidé une vérification — D7, lot 4a.

Une vérification d'identité ou de conflits décidée s'affichait « le … par
Me … », sans aucune provenance enregistrée : le jour où le connecteur peut
en inscrire une (lot 4b), l'inscription de Claude se serait lue, à l'écran,
exactement comme l'attestation du juriste. Désormais :

* une inscription PRÉSUMÉE (Claude, non confirmée) porte un badge AMBRE
  « Vérifié (présumé) », jamais le vert d'une identité vérifiée, et la
  mention « inscrit par Claude le … — à confirmer » — le nom du signataire
  n'y est JAMAIS accolé ;
* le bouton « Confirmer » (POST, CSRF, etag) est le seul chemin qui en fait
  l'attestation du juriste ;
* une confirmation se lit « confirmé le … par Me … (inscrit par Claude le …) ».

Vraies routes, vrais gabarits, faux Firestore partagé ; on relit ce que le
navigateur reçoit et ce qui est STOCKÉ.
"""

import os
import pathlib
import re
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
    import dav.sync as dav_sync  # its db is patched below
    from models import dossier as dossier_model
    from models import partie as pm
    import routes.doc_templates as doc_templates_routes
    import routes.dossiers as dossiers_routes
    import routes.parties as parties_routes
    import routes.reception as reception_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import kyc  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it.
_LOADED_UNDER_FAKE = (dav_sync, dossier_model)

UTC = timezone.utc
SIGNER = "Me Jason Test"
INSCRIBED = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
CONFIRMED = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(parties_routes, "_compliance_signer", lambda: SIGNER)
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(db):
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
    for bp in (parties_routes.parties_bp, dossiers_routes.dossiers_bp,
               doc_templates_routes.doc_templates_bp,
               reception_routes.reception_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _seed(db, pid="p1", **over):
    doc = {**pm._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": "Jean",
           "last_name": "Tremblay", "etag": f"e-{pid}",
           "created_at": INSCRIBED, "updated_at": INSCRIBED}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)
    return pid


PRESUMED = dict(identity_verified="vérifié", identity_verified_date=INSCRIBED,
                identity_verified_source="mcp")
CONFIRMED_BY_LAWYER = dict(
    identity_verified="vérifié", identity_verified_date=INSCRIBED,
    identity_verified_source="mcp", identity_verified_confirmed_at=CONFIRMED,
    identity_verified_confirmed_by="juriste")
LAWYERS = dict(identity_verified="exempté", identity_verified_date=INSCRIBED,
               identity_verified_source="")


def _conformite(html: str) -> str:
    """The Conformité card only — its end is the next card's heading."""
    start = html.index(">Conformité</h2>")
    end = html.index("Dossiers associés", start)
    return html[start:end]


def _identite(html: str) -> str:
    block = _conformite(html)
    return block[block.index("Identité"):block.index("Conflit d")]


# ══════════════════════════════════════════════════════════════════════
# 1. Le badge et la mention
# ══════════════════════════════════════════════════════════════════════


def test_a_presumed_inscription_is_amber_and_never_signed(client, db):
    pid = _seed(db, **PRESUMED)

    html = client.get(f"/parties/{pid}").get_data(as_text=True)

    block = _identite(html)
    assert "Vérifié (présumé)" in block
    assert "bg-amber-100 text-amber-700" in block
    assert "bg-green-100" not in block
    assert "inscrit par Claude le 25 septembre 2026 — à confirmer" in block
    assert SIGNER not in _conformite(html)


def test_a_presumed_conflict_keeps_the_red_of_an_alarm(client, db):
    """Amber is the colour of « Vérifié (présumé) »: a conflict Claude
    DETECTED painted amber would read, at a glance, like Claude's all-clear.
    Under-warning on a bar to acting is the failure that costs — the red
    stays, the « (présumé) » and the « à confirmer » say who inscribed it.
    (Failed on f5033ec: every presumed status was amber.)"""
    pid = _seed(db, conflict_check="conflit_détecté",
                conflict_check_date=INSCRIBED, conflict_check_source="mcp")

    html = client.get(f"/parties/{pid}").get_data(as_text=True)

    block = _conformite(html)
    block = block[block.index("Conflit d"):]
    assert "Conflit détecté (présumé)" in block
    assert "bg-red-100 text-red-700" in block
    assert "bg-amber-100" not in block
    assert "inscrit par Claude le 25 septembre 2026 — à confirmer" in block
    assert SIGNER not in block
    assert "/conformite/conflit/confirmer" in html


def test_the_confirm_form_carries_csrf_and_the_etag(client, db):
    pid = _seed(db, **PRESUMED)
    html = client.get(f"/parties/{pid}").get_data(as_text=True)
    form = re.search(
        r'<form method="post"\s+action="/parties/p1/conformite/identite/'
        r'confirmer"[^>]*>(.*?)</form>', html, re.S)
    assert form, "no Confirmer form"
    assert 'name="csrf_token" value="tok"' in form.group(1)
    assert 'name="expected_etag" value="e-p1"' in form.group(1)
    # Only the presumed check offers it.
    assert "/conformite/conflit/confirmer" not in html


def test_a_confirmed_inscription_names_both_acts(client, db):
    pid = _seed(db, **CONFIRMED_BY_LAWYER)
    block = _identite(client.get(f"/parties/{pid}").get_data(as_text=True))
    assert "bg-green-100 text-green-700" in block and "présumé" not in block
    assert (f"confirmé le 27 septembre 2026 par {SIGNER} "
            "(inscrit par Claude le 25 septembre 2026)") in block
    assert "Confirmer" not in block


def test_the_lawyer_s_own_attestation_reads_as_before(client, db):
    pid = _seed(db, **LAWYERS)
    block = _identite(client.get(f"/parties/{pid}").get_data(as_text=True))
    assert "Exempté" in block and "bg-blue-100 text-blue-700" in block
    assert f"le 25 septembre 2026 par {SIGNER}" in block
    assert "Claude" not in block


def test_a_decided_status_without_a_date_never_breaks_the_fiche(client, db):
    """A contact created with a decided status before lot 4a has no date:
    the fiche renders the status, with no dated attribution."""
    pid = _seed(db, identity_verified="vérifié", identity_verified_date=None)
    resp = client.get(f"/parties/{pid}")
    assert resp.status_code == 200
    block = _identite(resp.get_data(as_text=True))
    assert "Vérifié" in block and " par " not in block


def test_a_presumed_status_without_a_date_still_says_claude(client, db):
    pid = _seed(db, identity_verified="vérifié", identity_verified_date=None,
                identity_verified_source="mcp")
    block = _identite(client.get(f"/parties/{pid}").get_data(as_text=True))
    assert "inscrit par Claude — à confirmer" in block
    assert SIGNER not in block


def test_the_section_shows_for_a_dossier_client_of_another_role(client, db):
    """The coverage report checks EVERY dossier client, whatever the
    contact's role — the fiche must show what it checks."""
    pid = _seed(db, contact_role="autre")
    db.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                            "title": "T", "status": "actif",
                            "clients": [{"id": pid, "name": "Jean Tremblay"}],
                            "client_ids": [pid]})
    html = client.get(f"/parties/{pid}").get_data(as_text=True)
    assert ">Conformité</h2>" in html

    other = _seed(db, pid="p2", contact_role="autre")
    assert ">Conformité</h2>" not in client.get(
        f"/parties/{other}").get_data(as_text=True)


# ══════════════════════════════════════════════════════════════════════
# 2. « Confirmer »
# ══════════════════════════════════════════════════════════════════════


def _ctag(db):
    return (db.peek("dav_sync/parties") or {}).get("ctag")


def test_confirming_turns_the_inscription_into_the_attestation(client, db):
    pid = _seed(db, **PRESUMED)
    before = _ctag(db)

    resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                       data={"expected_etag": "e-p1"})

    assert resp.status_code == 302
    assert "message=V%C3%A9rification+confirm%C3%A9e" in resp.headers["Location"]
    stored = db.peek(f"parties/{pid}")
    assert stored["identity_verified_confirmed_by"] == "juriste"
    assert kyc.is_decided(stored, kyc.FIELD_IDENTITY)
    assert _ctag(db) != before  # the etag moved: DavX5 must re-sync
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Vérification confirmée." in page


def test_a_stale_confirmation_is_refused_with_a_banner(client, db):
    pid = _seed(db, **PRESUMED)
    resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                       data={"expected_etag": "e-autre-version"})
    assert resp.status_code == 302 and "erreur=" in resp.headers["Location"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "La fiche a changé depuis son affichage" in page
    assert kyc.is_presumed(db.peek(f"parties/{pid}"), kyc.FIELD_IDENTITY)


def test_a_confirmation_without_the_version_is_refused(client, db):
    pid = _seed(db, **PRESUMED)
    resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                       data={})
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "La version de la fiche est requise pour confirmer" in page
    assert kyc.is_presumed(db.peek(f"parties/{pid}"), kyc.FIELD_IDENTITY)


def test_nothing_presumed_is_nothing_to_confirm(client, db):
    pid = _seed(db, **LAWYERS)
    resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                       data={"expected_etag": "e-p1"})
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Rien à confirmer" in page


def test_an_unknown_check_or_contact_goes_to_the_list(client, db):
    pid = _seed(db, **PRESUMED)
    for path in (f"/parties/{pid}/conformite/autre/confirmer",
                 "/parties/p9/conformite/identite/confirmer"):
        resp = client.post(path, data={"expected_etag": "e-p1"})
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/parties/")


def test_the_confirm_route_is_a_post_only(client, db):
    pid = _seed(db, **PRESUMED)
    assert client.get(
        f"/parties/{pid}/conformite/identite/confirmer").status_code == 405


# ══════════════════════════════════════════════════════════════════════
# 3. Le formulaire du juriste
# ══════════════════════════════════════════════════════════════════════


def _form_post(pid, db, **over):
    stored = db.peek(f"parties/{pid}")
    data = {
        "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay",
        "identity_verified": stored["identity_verified"],
        "conflict_check": stored["conflict_check"],
        "expected_etag": stored["etag"],
    }
    data.update(over)
    return data


def test_a_form_resave_keeps_the_inscription_presumed(client, db):
    """Re-saving the fiche for an unrelated field never confirms."""
    pid = _seed(db, **PRESUMED)
    resp = client.post(f"/parties/{pid}",
                       data=_form_post(pid, db, email="jean@exemple.com"))
    assert resp.status_code == 302
    stored = db.peek(f"parties/{pid}")
    assert stored["email"] == "jean@exemple.com"
    assert kyc.is_presumed(stored, kyc.FIELD_IDENTITY)


def test_a_form_status_change_is_the_lawyer_s_decision(client, db):
    pid = _seed(db, **PRESUMED)
    client.post(f"/parties/{pid}",
                data=_form_post(pid, db, identity_verified="exempté"))
    stored = db.peek(f"parties/{pid}")
    assert stored["identity_verified_source"] == "juriste"
    assert kyc.is_decided(stored, kyc.FIELD_IDENTITY)


def test_the_edit_form_says_the_status_is_presumed(client, db):
    pid = _seed(db, **PRESUMED)
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert "Inscrit par Claude — à confirmer depuis la fiche." in html
    assert html.count("Inscrit par Claude") == 1  # the identity select only

    lawyer = _seed(db, pid="p2", **LAWYERS)
    assert "Inscrit par Claude" not in client.get(
        f"/parties/{lawyer}/edit").get_data(as_text=True)


def test_the_edit_form_offers_conformite_to_a_dossier_client(client, db):
    """A dossier client whose contact role is not « client »: the fiche
    shows its Conformité, so the form must let the lawyer decide it."""
    pid = _seed(db, contact_role="autre", **PRESUMED)
    db.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                            "title": "T", "status": "actif",
                            "clients": [{"id": pid, "name": "Jean Tremblay"}],
                            "client_ids": [pid]})
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert "isDossierClient: true," in html
    assert "contactRole === 'client' || isDossierClient" in html

    other = _seed(db, pid="p2", contact_role="autre")
    assert "isDossierClient: false," in client.get(
        f"/parties/{other}/edit").get_data(as_text=True)


def test_the_new_contact_form_renders_without_a_stored_record(client, db):
    assert client.get("/parties/new").status_code == 200


def test_a_web_creation_with_a_decided_status_is_the_lawyer_s(client, db):
    resp = client.post("/parties/", data={
        "type": "individual", "contact_role": "client",
        "last_name": "Roy", "identity_verified": "vérifié",
        "conflict_check": "non_vérifié",
    })
    assert resp.status_code == 302
    (stored,) = [p for p in db.peek_collection("parties").values()
                 if p.get("last_name") == "Roy"]
    assert stored["identity_verified_source"] == "juriste"
    assert stored["identity_verified_date"] is not None


def test_the_list_dot_stays_on_a_presumed_identity(client, db):
    _seed(db, **PRESUMED)
    _seed(db, pid="p2", last_name="Roy", **LAWYERS)
    html = client.get("/parties/?role=client",
                      headers={"HX-Request": "true"}).get_data(as_text=True)
    assert html.count("bg-orange-400") == 1
    assert 'title="Identité inscrite par Claude — à confirmer"' in html


# ══════════════════════════════════════════════════════════════════════
# 4. Les classes du balisage existent dans l'artefact compilé
# ══════════════════════════════════════════════════════════════════════


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def test_the_new_markup_uses_only_compiled_classes(client, db):
    """A class absent from the compiled artifact silently does not apply,
    and adding one is a seven-file fan-out (CLAUDE.md item 6)."""
    pid = _seed(db, **PRESUMED)
    snippets = [
        _conformite(client.get(f"/parties/{pid}").get_data(as_text=True)),
    ]
    edit = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    i = edit.index("Inscrit par Claude")
    snippets.append(edit[max(0, i - 200):i + 50])
    classes = {c for s in snippets
               for block in re.findall(r'class="([^"]+)"', s)
               for c in block.split()}
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert classes and not absent, absent
