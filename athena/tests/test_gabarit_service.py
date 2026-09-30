"""Generation from a gabarit — ONE assembly for the web popup and, later,
the connector (lot 2A, step T4).

``services/gabarits.py`` is the generation logic hoisted out of
``routes/doc_templates.py``: slot resolution, the field inventory, value
collection with each field's REAL ceiling, the fill, and the save into the
dossier's « Projets » folder. The route became a request adapter over it.

Four families of tests, all over the REAL routes, templates and models on
the shared fake Firestore and the realistic fake Cloud Storage — what they
read back is what the STORE holds, never a dict handed to a mock:

1. **Web behaviour pins.** Written and run green on ``c81a467`` BEFORE the
   hoist, they pin what the popup renders and what a generation stores:
   the refactor must not move them (the equivalence the task asks for).
2. **The documented refusals** — each fails on the old route: a slot that
   is not on the dossier (it used to fall back to the first party, and a
   document was produced on a choice the lawyer had not made), a contact
   that no longer exists, a value past its ceiling (it used to be CUT at
   2 000 characters in silence — a one-paragraph ``{{sommaire}}`` of 3 000
   lost its last third).
3. **The service itself**: the strict/lenient slot matrix, the inventory
   that carries resolved FLAGS and never a value, the ceilings, the fill
   report, the save that refuses before writing anything.
4. **Route ≡ service**: on the same inputs, the bytes the route stores are
   the bytes the service produces.
"""

import html as html_module
import io
import os
import sys
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.doc_template as tpl_model
    import models.document as document_model
    import models.folder as folder_model
    import routes.doc_templates as dt
    import routes.documents as rd

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from utils.docx_fill import fill_docx  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402
from utils.template_fields import fallback_value, manual_value  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (document_model,)

UTC = timezone.utc
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
BLANK = "\n\n"
LONG_SOMMAIRE = ("Le demandeur allègue un manquement contractuel. " * 70).strip()
assert 3000 < len(LONG_SOMMAIRE) < 5000 and "\n" not in LONG_SOMMAIRE

PLACEHOLDERS = [
    "dossier.titre",
    "client.nom_complet",
    "adverse.nom_complet",
    "destinataire.nom_complet",
    "dossier.sommaire",
    "dossier.defendeurs_avec_adresse",
    "objet_lettre",
    "privilège",
    "civilité",
]


def _docx(names) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{{{{{n}}}}}</w:t></w:r></w:p>" for n in names)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document {_W}><w:body>{body}'
                    "</w:body></w:document>")
    return buf.getvalue()


TEMPLATE_BYTES = _docx(PLACEHOLDERS)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _individual(first, last, *, role="client", street="", city=""):
    return {
        "id": "", "type": "individual", "contact_role": role,
        "first_name": first, "last_name": last,
        "address_street": street, "address_unit": "", "address_city": city,
        "address_province": "Québec", "address_postal_code": "H1A 1A1",
        "address_country": "Canada",
    }


def _organization(name, *, role="partie_adverse", street="", city=""):
    return {
        "id": "", "type": "organization", "contact_role": role,
        "organization_name": name,
        "work_address_street": street, "work_address_unit": "",
        "work_address_city": city, "work_address_province": "Québec",
        "work_address_postal_code": "H7A 2B2", "work_address_country": "Canada",
    }


PARTIES = {
    "c1": _individual("Jean", "Tremblay", street="1 rue A", city="Montréal"),
    "c2": _individual("Marie", "Lavoie", street="2 rue B", city="Laval"),
    "a1": _organization("Alpha inc.", street="3 rue C", city="Québec"),
    "a2": _organization("Bêta ltée", street="4 rue D", city="Gatineau"),
    "x9": _individual("Paul", "Gagnon", role="avocat_adverse",
                      street="5 rue E", city="Sherbrooke"),
    "cx": _individual("Zoé", "Roy", street="6 rue F", city="Lévis"),
}


def _entry(pid, roles):
    return {"id": pid, "name": f"{PARTIES[pid].get('first_name', '')} "
            f"{PARTIES[pid].get('last_name', '') or PARTIES[pid].get('organization_name', '')}".strip(),
            "roles": roles, "avocat_id": "", "avocat_name": ""}


@pytest.fixture
def store(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: bucket)
    for pid, data in PARTIES.items():
        db.seed(f"parties/{pid}", {**data, "id": pid})
    base = {"status": "actif", "forum_type": "judiciaire", "role": "demandeur",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
    db.seed("dossiers/d1", {
        **base, "id": "d1", "file_number": "2026-001",
        "title": "Tremblay c. Alpha", "sommaire": "Court sommaire.",
        "clients": [_entry("c1", ["demandeur"]), _entry("c2", ["demandeur"])],
        "client_ids": ["c1", "c2"],
        "opposing_parties": [_entry("a1", ["défendeur"]), _entry("a2", ["défendeur"])],
        "opposing_party_ids": ["a1", "a2"],
    })
    db.seed("dossiers/d2", {
        **base, "id": "d2", "file_number": "2026-002", "title": "Roy",
        "sommaire": LONG_SOMMAIRE,
        "clients": [_entry("cx", ["demandeur"])], "client_ids": ["cx"],
        "opposing_parties": [], "opposing_party_ids": [],
    })
    template, errors = tpl_model.create_template(
        io.BytesIO(TEMPLATE_BYTES), "lettre.docx", len(TEMPLATE_BYTES),
        {"name": "Lettre", "category": "correspondance", "kind": "gabarit"}, UID,
    )
    assert errors == [], errors
    return db, bucket, template


@pytest.fixture
def web(store):
    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl, phone=format_phone_display,
                                 jsattr=lambda v: v)
    app.register_blueprint(dt.doc_templates_bp)
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


class _Form(HTMLParser):
    """The form state a browser would submit: selects' selected option,
    inputs' value, textareas' text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.kinds: dict[str, str] = {}
        self.maxlength: dict[str, str] = {}
        self._select = None
        self._first_option = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select":
            self._select = a.get("name")
            self._first_option = None
        elif tag == "option" and self._select:
            if self._first_option is None:
                self._first_option = a.get("value", "")
                self.values.setdefault(self._select, self._first_option)
            if "selected" in a:
                self.values[self._select] = a.get("value", "")
            self.kinds[self._select] = "select"
        elif tag == "input" and a.get("name") and a.get("type") in ("hidden", "text"):
            self.values[a["name"]] = a.get("value", "")
            self.kinds[a["name"]] = a.get("type")
            if "maxlength" in a:
                self.maxlength[a["name"]] = a["maxlength"]
        elif tag == "textarea":
            self._textarea = a.get("name")
            self.values[self._textarea] = ""
            self.kinds[self._textarea] = "textarea"
            self.maxlength[self._textarea] = a.get("maxlength", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self.values[self._textarea] += data


def _form(html: str) -> _Form:
    parser = _Form()
    parser.feed(html)
    return parser


def _fields_url(template_id, **params):
    query = "&".join(f"{k}={v}" for k, v in {"template_id": template_id, **params}.items())
    return f"/gabarits/generer/champs?{query}"


def _stored_documents(db):
    return [d for d in db.peek_collection("documents").values()]


def _stored_bytes(bucket, doc):
    return bucket.objects[doc["storage_path"]].data


def _document_xml(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read("word/document.xml").decode("utf-8")


# ══════════════════════════════════════════════════════════════════════
# 1. Web behaviour pins (green on c81a467, before the hoist)
# ══════════════════════════════════════════════════════════════════════


def test_the_popup_defaults_each_slot_to_the_dossiers_first_party(web, store):
    _, _, template = store
    html = web.get(_fields_url(template["id"], dossier_id="d1")).get_data(as_text=True)
    form = _form(html)
    assert form.values["client_id"] == "c1"
    assert form.values["adverse_id"] == "a1"
    assert form.values["champ__client.nom_complet"] == "Jean Tremblay"
    assert form.values["champ__adverse.nom_complet"] == "Alpha inc."
    assert form.values["champ__dossier.titre"] == "Tremblay c. Alpha"
    # The party block has a blank line → a textarea (its newlines survive).
    assert form.kinds["champ__dossier.defendeurs_avec_adresse"] == "textarea"
    assert BLANK in form.values["champ__dossier.defendeurs_avec_adresse"]
    # Manual fields are prompted; passthrough is only LISTED.
    assert form.kinds["champ__privilège"] == "select"
    assert form.values["champ__objet_lettre"] == ""
    assert "champ__civilité" not in form.values
    assert "civilité" in html


def test_the_popup_honours_a_chosen_slot(web, store):
    _, _, template = store
    form = _form(web.get(_fields_url(
        template["id"], dossier_id="d1", client_id="c2", adverse_id="a2",
        destinataire_id="x9",
    )).get_data(as_text=True))
    assert form.values["client_id"] == "c2"
    assert form.values["adverse_id"] == "a2"
    assert form.values["destinataire_id"] == "x9"
    assert form.values["champ__client.nom_complet"] == "Marie Lavoie"
    assert form.values["champ__adverse.nom_complet"] == "Bêta ltée"
    assert form.values["champ__destinataire.nom_complet"] == "Paul Gagnon"


def test_switching_dossier_resets_a_selection_carried_from_the_previous_one(web, store):
    """The dossier picker re-renders the modal with ``hx-include`` of the
    slot inputs, so the PREVIOUS dossier's client arrives with the new
    dossier: the popup resets it to the new dossier's first client — and
    SHOWS that choice before anything is generated."""
    _, _, template = store
    html = web.get(
        f"/gabarits/generer?template_id={template['id']}&set_dossier_id=d2"
        "&dossier_id=d1&client_id=c2&adverse_id=a2"
    ).get_data(as_text=True)
    form = _form(html)
    assert form.values["dossier_id"] == "d2"
    assert form.values["client_id"] == "cx"
    assert form.values["champ__client.nom_complet"] == "Zoé Roy"
    assert form.values.get("champ__adverse.nom_complet", "") == ""


def test_an_unknown_destinataire_is_dropped_from_the_popup(web, store):
    _, _, template = store
    form = _form(web.get(_fields_url(
        template["id"], dossier_id="d1", destinataire_id="gone",
    )).get_data(as_text=True))
    assert form.values["destinataire_id"] == ""
    assert form.values["champ__destinataire.nom_complet"] == ""


def _submission(form: _Form, **overrides) -> dict:
    data = {k: v for k, v in form.values.items() if k != "q"}
    data.update(overrides)
    return data


def _expected_values(submitted: dict) -> dict:
    """What the route fills with — computed HERE, independently of it."""
    auto = ["dossier.titre", "client.nom_complet", "adverse.nom_complet",
            "destinataire.nom_complet", "dossier.sommaire",
            "dossier.defendeurs_avec_adresse"]
    values = {}
    for name in auto:
        raw = submitted.get(f"champ__{name}", "").strip()
        values[name] = raw or fallback_value(name, is_auto=True)
    for name in ("objet_lettre", "privilège"):
        values[name] = manual_value(name, submitted.get(f"champ__{name}", ""))
    return values


def test_a_generation_stores_exactly_what_the_form_carried(web, store):
    db, bucket, template = store
    form = _form(web.get(_fields_url(
        template["id"], dossier_id="d1", client_id="c2", destinataire_id="x9",
    )).get_data(as_text=True))
    submitted = _submission(
        form, **{"champ__objet_lettre": "Mise en demeure",
                 "champ__privilège": "SANS PRÉJUDICE",
                 "champ__dossier.titre": "Titre retouché à la main"})
    resp = web.post("/gabarits/generer", data=submitted,
                    headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Document généré" in resp.get_data(as_text=True)

    (doc,) = _stored_documents(db)
    assert doc["dossier_id"] == "d1"
    assert doc["category"] == "correspondance"
    assert doc["tags"] == ["gabarit"]
    assert doc["genere_depuis"] == "Généré depuis le gabarit «Lettre» v1"
    assert doc["display_name"].startswith("2026-001 - ")
    assert doc["display_name"].endswith(" - Projet Lettre")
    assert doc["storage_path"].startswith(f"users/{UID}/dossiers/d1/documents/")
    projets = db.peek(f"folders/{doc['folder_id']}") or {}
    assert projets.get("system_role") == folder_model.SYSTEM_ROLE_PROJETS
    # The bytes are the template filled with the SUBMITTED values — an edit
    # the lawyer made in the popup (the title) is honoured, never replaced.
    expected = fill_docx(TEMPLATE_BYTES, _expected_values(submitted))
    assert _document_xml(_stored_bytes(bucket, doc)) == _document_xml(expected)
    assert "Titre retouché à la main" in _document_xml(expected)


def test_a_blank_auto_field_prints_the_loud_marker(web, store):
    db, bucket, template = store
    form = _form(web.get(_fields_url(template["id"], dossier_id="d1"))
                 .get_data(as_text=True))
    submitted = _submission(form, **{"champ__dossier.titre": ""})
    web.post("/gabarits/generer", data=submitted, headers={"HX-Request": "true"})
    (doc,) = _stored_documents(db)
    xml = _document_xml(_stored_bytes(bucket, doc))
    assert "[CHAMP MANQUANT : dossier.titre]" in xml


def test_without_a_dossier_the_document_is_downloaded_not_stored(web, store):
    db, _, template = store
    resp = web.post("/gabarits/generer", data={
        "template_id": template["id"], "dossier_id": "",
        "champ__objet_lettre": "Objet",
    })
    assert resp.status_code == 200
    assert resp.headers["Content-Disposition"].startswith("attachment")
    assert _stored_documents(db) == []
    assert "Objet" in _document_xml(resp.data)


def test_an_option_outside_the_list_is_refused_by_name(web, store):
    db, _, template = store
    form = _form(web.get(_fields_url(template["id"], dossier_id="d1"))
                 .get_data(as_text=True))
    resp = web.post("/gabarits/generer",
                    data=_submission(form, **{"champ__privilège": "INVENTÉ"}),
                    headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "privilège" in html_module.unescape(resp.get_data(as_text=True))
    assert _stored_documents(db) == []


def test_an_unknown_dossier_is_refused(web, store):
    db, _, template = store
    resp = web.post("/gabarits/generer",
                    data={"template_id": template["id"], "dossier_id": "gone"},
                    headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Le dossier sélectionné est introuvable" in html_module.unescape(
        resp.get_data(as_text=True))
    assert _stored_documents(db) == []


# ══════════════════════════════════════════════════════════════════════
# 2. The documented refusals — each FAILS on the old route
# ══════════════════════════════════════════════════════════════════════


def _render(web, template_id, **params):
    return _form(web.get(_fields_url(template_id, **params)).get_data(as_text=True))


def _nothing_written(db, bucket, template):
    """No document, no « Projets » folder, no object but the template's."""
    assert _stored_documents(db) == []
    assert db.peek_collection("folders") == {}
    assert set(bucket.objects) == {template["storage_path"]}


def _post(web, data, *, htmx=True):
    headers = {"HX-Request": "true"} if htmx else {}
    return web.post("/gabarits/generer", data=data, headers=headers)


@pytest.fixture
def events(monkeypatch):
    seen: list = []
    monkeypatch.setattr(dt, "log_template_event",
                        lambda event, **kw: seen.append((event, kw)))
    return seen


def test_a_client_that_is_not_on_the_dossier_is_refused_not_swapped(
    web, store, events,
):
    """The old POST never looked at the slot: a client removed from the
    dossier since the popup rendered — or a crafted form — generated on the
    values of a choice that no longer stood."""
    db, bucket, template = store
    form = _render(web, template["id"], dossier_id="d1")
    resp = _post(web, _submission(form, client_id="cx"))
    assert resp.status_code == 200                  # htmx swaps only a 2xx
    text = html_module.unescape(resp.get_data(as_text=True))
    assert "Le client choisi ne figure pas (ou plus) au dossier" in text
    assert "Zoé" not in text and "Roy" not in text  # never quotes the contact
    _nothing_written(db, bucket, template)
    assert ("generation_failed", {"template_id": template["id"],
                                  "reason": "slot_foreign", "slot": "client"}) in events


def test_an_opposing_party_that_is_not_on_the_dossier_is_refused(web, store):
    db, bucket, template = store
    form = _render(web, template["id"], dossier_id="d1")
    resp = _post(web, _submission(form, adverse_id="c1"))   # a CLIENT, not adverse
    assert "La partie adverse choisie ne figure pas" in html_module.unescape(
        resp.get_data(as_text=True))
    _nothing_written(db, bucket, template)


def test_a_destinataire_deleted_since_the_popup_rendered_is_refused(web, store):
    db, bucket, template = store
    form = _render(web, template["id"], dossier_id="d1", destinataire_id="x9")
    assert form.values["destinataire_id"] == "x9"
    db.external_delete("parties/x9")
    resp = _post(web, _submission(form))
    assert "Le destinataire choisi est introuvable" in html_module.unescape(
        resp.get_data(as_text=True))
    _nothing_written(db, bucket, template)


def test_a_long_one_paragraph_sommaire_prints_whole(web, store):
    """The popup offered it in a ``maxlength=2000`` input and the route CUT
    it at 2 000 characters: the letter lost its last third, in silence. Its
    ceiling is now the model's own (5 000), on both sides."""
    db, bucket, template = store
    form = _render(web, template["id"], dossier_id="d2")
    assert form.values["champ__dossier.sommaire"] == LONG_SOMMAIRE
    assert int(form.maxlength["champ__dossier.sommaire"]) >= len(LONG_SOMMAIRE)
    resp = _post(web, _submission(form))
    assert "Document généré" in resp.get_data(as_text=True)
    (doc,) = _stored_documents(db)
    assert LONG_SOMMAIRE in _document_xml(_stored_bytes(bucket, doc))


def test_a_manual_value_past_its_ceiling_is_refused_and_nothing_is_written(
    web, store, events,
):
    db, bucket, template = store
    form = _render(web, template["id"], dossier_id="d1")
    resp = _post(web, _submission(form, **{"champ__objet_lettre": "x" * 2001}))
    text = html_module.unescape(resp.get_data(as_text=True))
    assert "« objet_lettre » dépasse la longueur permise (2000 caractères)" in text
    _nothing_written(db, bucket, template)
    assert ("generation_failed", {"template_id": template["id"],
                                  "reason": "value_too_long"}) in events


def test_a_block_past_twenty_thousand_characters_is_refused(web, store):
    db, bucket, template = store
    form = _render(web, template["id"], dossier_id="d1")
    block = BLANK.join(["y" * 5000] * 5)
    resp = _post(web, _submission(
        form, **{"champ__dossier.defendeurs_avec_adresse": block}))
    assert "(20000 caractères)" in html_module.unescape(resp.get_data(as_text=True))
    _nothing_written(db, bucket, template)


def test_a_refused_direct_download_says_why_on_the_template_page(web, store):
    """The no-JS path is a plain POST: its refusals used to land on the
    template's page with no word at all."""
    db, _, template = store
    resp = _post(web, {"template_id": template["id"], "dossier_id": "",
                       "champ__objet_lettre": "x" * 2001}, htmx=False)
    assert resp.status_code == 302
    assert f"/gabarits/{template['id']}?erreur=" in resp.headers["Location"]
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert "dépasse la longueur permise" in html_module.unescape(page)
    assert _stored_documents(db) == []


# ══════════════════════════════════════════════════════════════════════
# 3. The service
# ══════════════════════════════════════════════════════════════════════

with mock.patch("google.cloud.firestore.Client"):
    import services.gabarits as sg
    from models import dossier as dossier_model

_DAY = datetime(2026, 9, 27).date()


@pytest.mark.parametrize("args, reason, field", [
    (("gone",), "dossier_not_found", "dossier_id"),
    (("d1", "cx"), "slot_foreign", "client_id"),
    (("d1", "", "c1"), "slot_foreign", "adverse_id"),
    (("", "", "a1"), "slot_foreign", "adverse_id"),       # no dossier to be on
    (("", "gone"), "slot_unknown", "client_id"),          # the free pick
    (("d1", "", "", "gone"), "slot_unknown", "destinataire_id"),
])
def test_strict_resolution_refuses_what_it_used_to_swap(store, args, reason, field):
    with pytest.raises(sg.GenerationRefused) as exc:
        sg.resolve_slots(*args)
    assert (exc.value.reason, exc.value.field) == (reason, field)
    assert exc.value.message


def test_strict_resolution_defaults_an_omitted_slot_and_honours_a_chosen_one(store):
    slots = sg.resolve_slots("d1")
    assert (slots.client_id, slots.adverse_id) == ("c1", "a1")
    assert slots.client["last_name"] == "Tremblay"
    assert set(slots.parties) == {"c1", "c2", "a1", "a2"}
    slots = sg.resolve_slots("d1", "c2", "a2", "x9")
    assert (slots.client_id, slots.adverse_id, slots.destinataire_id) == ("c2", "a2", "x9")
    # No dossier: the client slot is a free contact pick.
    assert sg.resolve_slots("", "x9").client_id == "x9"


def test_the_lenient_render_repair_is_the_pre_hoist_behaviour(store):
    slots = sg.resolve_slots("gone", "cx", "c1", "gone", lenient=True)
    assert (slots.dossier, slots.dossier_id, slots.adverse_id) == (None, "", "")
    assert (slots.client_id, slots.destinataire_id) == ("cx", "")  # free pick kept
    slots = sg.resolve_slots("d1", "cx", "c1", lenient=True)
    assert (slots.client_id, slots.adverse_id) == ("c1", "a1")     # reset, shown


def test_a_slot_the_dossier_vouches_for_is_not_refused_when_its_fiche_is_gone(store):
    db, _, _ = store
    db.external_delete("parties/c2")
    slots = sg.resolve_slots("d1", "c2")
    assert slots.client is None and slots.client_id == ""


def test_the_connector_can_ask_for_an_ambiguous_slot_to_be_named(store):
    with pytest.raises(sg.GenerationRefused) as exc:
        sg.resolve_slots("d1", required_slots={"client"}, refuse_ambiguous=True)
    assert (exc.value.reason, exc.value.field) == ("slot_ambiguous", "client_id")
    # One candidate is no ambiguity; a slot the template does not need is none either.
    assert sg.resolve_slots("d2", required_slots={"client"},
                            refuse_ambiguous=True).client_id == "cx"
    assert sg.resolve_slots("d1", adverse_id="a2", required_slots={"adverse"},
                            refuse_ambiguous=True).client_id == "c1"


def test_the_inventory_carries_resolved_flags_and_never_a_value(store):
    _, _, template = store
    slots = sg.resolve_slots("d1", "c1", "a1")
    inventory = sg.field_inventory(template, slots)
    by_name = {f.name: f for f in inventory.fields}
    assert [f.name for f in inventory.fields] == PLACEHOLDERS
    assert by_name["client.nom_complet"].kind == "auto"
    assert by_name["client.nom_complet"].resolved is True
    assert by_name["destinataire.nom_complet"].resolved is False   # no destinataire
    assert by_name["objet_lettre"].kind == "manual"
    assert by_name["privilège"].options                            # a select
    assert (by_name["civilité"].kind, by_name["civilité"].ceiling) == ("passthrough", 0)
    assert inventory.slots_required == ("adverse", "client", "destinataire", "dossier")
    # D5: nothing the server resolved leaves through the inventory.
    dump = repr(inventory)
    for leaked in ("Tremblay", "Alpha", "rue A", "Montréal"):
        assert leaked not in dump


def test_the_auto_ceiling_is_the_longest_stored_field_the_catalog_reads():
    assert sg.AUTO_MAX_CHARS == dossier_model._SOMMAIRE_MAX_LENGTH
    assert sg.MANUAL_MAX_CHARS == 2000
    assert sg.field_ceiling("auto", multiline=False) == 5000
    assert sg.field_ceiling("manual", multiline=False) == 2000
    assert sg.field_ceiling("manual", multiline=True) == 20000
    # What the server itself resolves is never refused.
    assert sg.field_ceiling("auto", multiline=False, resolved="z" * 7000) == 7000


def test_a_composite_the_server_resolves_longer_than_the_base_is_accepted():
    template = {"placeholders": ["dossier.demandeurs"]}
    long_value = "N" * 6000
    values, missing = sg.values_from_submission(
        template, {"dossier.demandeurs": long_value},
        resolved={"dossier.demandeurs": long_value})
    assert values["dossier.demandeurs"] == long_value and missing == 0
    with pytest.raises(sg.GenerationRefused):
        sg.values_from_submission(template, {"dossier.demandeurs": long_value})


def test_a_textarea_is_measured_as_the_browser_counts_it():
    """A browser submits a textarea's line breaks as CRLF but counts each as
    ONE character for maxlength: the server measures the same way."""
    template = {"placeholders": ["dossier.defendeurs_avec_adresse"]}
    lines = "\r\n".join(["w" * 99] * 200)            # 19 999 once CRLF → LF
    assert len(lines) > sg.MULTILINE_MAX_CHARS
    values, _ = sg.values_from_submission(
        template, {"dossier.defendeurs_avec_adresse": lines})
    assert values["dossier.defendeurs_avec_adresse"] == lines.strip()


def test_save_checks_the_uid_before_touching_anything(store):
    db, bucket, template = store
    with pytest.raises(sg.GenerationRefused) as exc:
        sg.save_into_projets(template=template, dossier=dossier_model.get_dossier("d1"),
                             filled=b"x", uid="unknown", today=_DAY)
    assert exc.value.reason == "save_failed"
    _nothing_written(db, bucket, template)


def test_save_refuses_without_projets_and_never_files_at_the_root(store, monkeypatch):
    db, bucket, template = store
    monkeypatch.setattr(sg, "ensure_system_folder",
                        lambda did, role: (None, [folder_model.READ_ERROR]))
    with pytest.raises(sg.GenerationRefused) as exc:
        sg.save_into_projets(template=template, dossier=dossier_model.get_dossier("d1"),
                             filled=TEMPLATE_BYTES, uid=UID, today=_DAY)
    assert (exc.value.reason, exc.value.message) == (
        "projets_unavailable", folder_model.READ_ERROR)
    _nothing_written(db, bucket, template)


def test_save_files_into_projets_under_the_uid_it_was_given(store):
    db, bucket, template = store
    filled, _ = sg.fill(TEMPLATE_BYTES, {"dossier.titre": "T"})
    doc = sg.save_into_projets(template=template, dossier=dossier_model.get_dossier("d1"),
                               filled=filled, uid=UID, today=_DAY)
    stored = db.peek(f"documents/{doc['id']}")
    assert stored["display_name"] == "2026-001 - 2026-09-27 - Projet Lettre"
    assert stored["genere_depuis"] == "Généré depuis le gabarit «Lettre» v1"
    assert stored["storage_path"].startswith(f"users/{UID}/")
    assert db.peek(f"folders/{stored['folder_id']}")["system_role"] == "projets"
    assert bucket.objects[stored["storage_path"]].data == filled


# ── The system folder a save names: by its ROLE (the default folder tree) ──


def test_save_generated_files_into_factures_under_mandat(store):
    """FACTURES — the note d'honoraires' folder — is « Mandat › Factures »,
    created with its parent on first use; « Projets » is never touched."""
    db, bucket, template = store
    filled, _ = sg.fill(TEMPLATE_BYTES, {"dossier.titre": "T"})
    doc, folder = sg.save_generated(
        dossier=dossier_model.get_dossier("d1"), filled=filled, uid=UID,
        display_name="Note", filename="note.docx", category="correspondance",
        genere_depuis="", folder=sg.FACTURES)
    stored = db.peek(f"documents/{doc['id']}")
    factures = folder_model.system_folder_id("d1", "factures")
    assert folder["id"] == stored["folder_id"] == factures
    assert folder["system_role"] == "factures"
    assert db.peek(f"folders/{factures}")["parent_folder_id"] == (
        folder_model.system_folder_id("d1", "mandat"))
    assert db.peek(f"folders/{folder_model.system_folder_id('d1', 'projets')}") is None


def test_save_into_factures_checks_the_uid_before_touching_anything(store):
    db, bucket, template = store
    with pytest.raises(sg.GenerationRefused) as exc:
        sg.save_generated(
            dossier=dossier_model.get_dossier("d1"), filled=b"x", uid="unknown",
            display_name="Note", filename="note.docx", category="autre",
            genere_depuis="", folder=sg.FACTURES)
    assert exc.value.reason == "save_failed"
    _nothing_written(db, bucket, template)


@pytest.mark.parametrize("errors, message", [
    ([folder_model.READ_ERROR], folder_model.READ_ERROR),
    ([], "Le dossier « Factures » est indisponible. Réessayez."),
])
def test_save_refuses_without_factures_and_never_files_at_the_root(
    store, monkeypatch, errors, message,
):
    db, bucket, template = store
    asked = []

    def _unavailable(did, role):
        asked.append(role)
        return None, list(errors)

    monkeypatch.setattr(sg, "ensure_system_folder", _unavailable)
    with pytest.raises(sg.GenerationRefused) as exc:
        sg.save_generated(
            dossier=dossier_model.get_dossier("d1"), filled=TEMPLATE_BYTES,
            uid=UID, display_name="Note", filename="note.docx",
            category="autre", genere_depuis="", folder=sg.FACTURES)
    assert (exc.value.reason, exc.value.message) == ("factures_unavailable", message)
    assert asked == [folder_model.SYSTEM_ROLE_FACTURES]
    assert sg.FACTURES_UNAVAILABLE == (
        "Le dossier « Factures » est indisponible. Réessayez.")
    _nothing_written(db, bucket, template)


def test_projets_keeps_its_sentinel_its_reason_and_its_message(store, monkeypatch):
    """The connector compares ``folder is PROJETS`` and calls
    ``ensure_projets``: the sentinel is the default of save_generated, its
    own object, and its refusal is still ``projets_unavailable``."""
    import inspect

    assert (inspect.signature(sg.save_generated).parameters["folder"].default
            is sg.PROJETS)
    assert sg.PROJETS is not sg.FACTURES
    assert sg.PROJETS.role == folder_model.SYSTEM_ROLE_PROJETS
    assert sg.FACTURES.role == folder_model.SYSTEM_ROLE_FACTURES
    monkeypatch.setattr(sg, "ensure_system_folder", lambda did, role: (None, []))
    with pytest.raises(sg.GenerationRefused) as exc:
        sg.ensure_projets("d1")
    assert (exc.value.reason, exc.value.message) == (
        "projets_unavailable", sg.PROJETS_UNAVAILABLE)
    assert sg.PROJETS_UNAVAILABLE == (
        "Le dossier « Projets » est indisponible. Réessayez.")
    with pytest.raises(ValueError):
        sg._SystemFolderChoice("inconnu")


def _one_paragraph_docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document {_W}><w:body><w:p><w:r>'
                    f"<w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def test_fill_reports_the_rich_values_it_could_not_format():
    _, demoted = sg.fill(_one_paragraph_docx("{{corps}}"), {},
                         rich_values={"corps": "**gras**"})
    assert demoted == []
    filled, demoted = sg.fill(_one_paragraph_docx("Voir : {{corps}}"), {},
                              rich_values={"corps": "**gras**"})
    assert demoted == ["corps"]
    assert "**gras**" in _document_xml(filled)       # the sigils it explains


def test_fill_turns_an_invalid_template_into_a_refusal():
    with pytest.raises(sg.GenerationRefused) as exc:
        sg.fill(b"not a zip", {})
    assert exc.value.reason == "template_invalid"


# ══════════════════════════════════════════════════════════════════════
# 4. Route ≡ service, on the same inputs
# ══════════════════════════════════════════════════════════════════════


def test_the_popup_prefills_exactly_what_the_service_resolves(web, store):
    _, _, template = store
    form = _render(web, template["id"], dossier_id="d1", client_id="c2",
                   adverse_id="a2", destinataire_id="x9")
    resolved = sg.resolve_auto_values(template, sg.resolve_slots("d1", "c2", "a2", "x9"))
    assert resolved
    for name, value in resolved.items():
        assert form.values[f"champ__{name}"].replace("\r\n", "\n") == value


def test_the_route_stores_the_bytes_the_service_produces(web, store):
    db, bucket, template = store
    form = _render(web, template["id"], dossier_id="d1", client_id="c2",
                   destinataire_id="x9")
    submitted = _submission(form, **{"champ__objet_lettre": "Objet",
                                     "champ__privilège": "SANS PRÉJUDICE"})
    _post(web, submitted)
    (doc,) = _stored_documents(db)

    slots = sg.resolve_slots("d1", submitted["client_id"], submitted["adverse_id"],
                             submitted["destinataire_id"])
    values, _ = sg.values_from_submission(
        template,
        {n: submitted.get(f"champ__{n}", "") for n in template["placeholders"]},
        resolved=sg.resolve_auto_values(template, slots),
    )
    filled, _ = sg.fill(TEMPLATE_BYTES, values)
    assert _stored_bytes(bucket, doc) == filled


def test_every_popup_input_carries_the_ceiling_the_server_enforces(web, store):
    """The browser stops a user edit at ``maxlength``; the server refuses
    past ``field_ceiling``. Derived per rendered field, so the two numbers
    cannot drift apart."""
    _, _, template = store
    form = _render(web, template["id"], dossier_id="d2")
    fields = sg.form_fields(template, sg.resolve_auto_values(
        template, sg.resolve_slots("d2")))
    rendered = {f["name"]: f for f in fields}
    assert set(form.maxlength) == {f"champ__{n}" for n, f in rendered.items()
                                   if not f["options"]}
    for name, spec in rendered.items():
        if spec["options"]:
            continue                                    # a <select>
        assert int(form.maxlength[f"champ__{name}"]) == spec["ceiling"]
    assert rendered["objet_lettre"]["ceiling"] == sg.MANUAL_MAX_CHARS
    assert rendered["dossier.sommaire"]["ceiling"] == sg.AUTO_MAX_CHARS
