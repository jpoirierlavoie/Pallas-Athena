"""The web interface of the four administration natures outside the results
(2026-10-07): the lawyer's « Prélèvement » and « Apport », and the
« Virement interne » whose « Sens » names one of two stored kinds.

What the routes and templates add on top of the model's rules
(``tests/test_admin_natures.py``):

1. **The form.** Its « Type » options come from the model (``FORM_KINDS`` on
   a create; on an edit, the stored kind and the passages ``KIND_MOVES``
   permits — never one the model refuses), « Virement interne » + « Sens »
   become the stored code, and the x-data carries the type, the sens and
   the dossier through the REAL ``jsattr`` (``utils/html_attr``).
2. **The refusals.** A virement posted without a valid sens is refused at
   400 — one ``sens_requis`` line, nothing written, the submitted etag kept
   on an edit — and the fields the form hides for a nature (category,
   split, invoice, and a prélèvement's or an apport's dossier) are
   neutralized, so a stale value never turns into the model's refusal.
3. **What the pages say.** A badge on each nature; the « Avoir de
   l'avocat » in the journal header (full render and the HTMX fragment's
   out-of-band header) and on the account page (calendar year), absent over
   a truncated read and « indisponible » — never 0 — over a failed one; a
   « Nature » column in the CSV; the nature in the PDF's empty « Catégorie »
   cell and an avoir line after the tax line, only over a register read
   whole; the motif of a revision and « par script ».
4. **Compiled classes only** (CLAUDE.md item 6), on the model of
   ``tests/test_register_provenance_display.py``.

Every « Regression » test fails on the web code before the natures (checked
against it); the others are witnesses, and each guard they pin was proved
by removing it. The bench is the shared fake Firestore
(``tests/_fake_firestore.py``) under the real routes, models and templates;
« today » is frozen on Montréal's clock. Every amount, name and number here
is synthetic.
"""

import csv
import html
import io
import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    import routes.admin_ledger as ra
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests.test_kyc_rendering import _escape_css  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import admin_journal_pdf as ajp  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
NATURES = al.NON_RESULT_KINDS
SENS_REQUIS = "Indiquez le sens du virement interne : sortant ou entrant."
_ETAG_INPUT = re.compile(r'name="expected_etag" value="([^"]*)"')
# The spaces format_cents_fr and the PDF text extraction may use.
_SPACES = ("\N{NO-BREAK SPACE}", "\N{NARROW NO-BREAK SPACE}")


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fmt(cents: int) -> str:
    return format_cents_fr(cents)


# ══════════════════════════════════════════════════════════════════════
# The bench
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, as the sibling
    benches do, so a model a route starts to read cannot reach the mocked
    client instead."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def clock(monkeypatch):
    """Freeze the ONE Montréal clock read (``utils.deadlines.today_mtl``):
    the model refuses a future date on it, and the account page's calendar
    year ends on it."""
    from utils import deadlines as dl

    frozen = datetime(2031, 9, 20, 16, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    return frozen


@pytest.fixture
def fake(monkeypatch, clock):
    f = install(monkeypatch, *_fake_modules())
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2031-001", "title": "Dossier fictif",
        "status": "actif",
    })
    return f


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        # main.py's filter — never the identity stub other benches use,
        # under which an x-data value renders as escaped text, not JSON.
        jsattr=jsattr,
    )
    for bp in (trust_routes.trust_bp, invoices_routes.invoices_bp,
               ra.admin_bp, dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _data(kind: str, **over) -> dict:
    """An entry as the MODEL's door receives it (the seed path)."""
    d = {
        "account_id": "ops1", "kind": kind, "amount": 10000,
        "method": "virement", "counterparty": "Contrepartie fictive",
        "date": _d(2031, 9, 1), "description": "", "reference": "",
        "supplier_invoice_ref": "",
    }
    if kind == "dépense":
        d["category"] = "loyer"
    d.update(over)
    return d


def _create(kind: str, **over) -> dict:
    entry, errs = al.create_transaction(_data(kind, **over))
    assert errs == [], errs
    return entry


def _form(kind: str, **over) -> dict:
    """The entry form as the browser posts it."""
    f = {
        "account_id": "ops1", "kind": kind, "amount": "100,00",
        "method": "virement", "counterparty": "Compte en fidéicommis",
        "date": "2031-09-01", "description": "", "reference": "",
        "supplier_invoice_ref": "",
    }
    f.update(over)
    return f


def _entries(fake) -> dict:
    return fake.peek_collection("admin_transactions")


def _path(entry: dict) -> str:
    return f"admin_transactions/{entry['id']}"


class _Selects(HTMLParser):
    """Every ``<select>`` of a page by name: its attributes and its options
    (value, label, attributes), in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.selects: dict = {}
        self._select = None
        self._option = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "select":
            self._select = {"attrs": attributes, "options": []}
            self.selects[attributes.get("name", "")] = self._select
        elif tag == "option" and self._select is not None:
            self._option = {"value": attributes.get("value", ""), "label": "",
                            "attrs": attributes}
            self._select["options"].append(self._option)

    def handle_data(self, data):
        if self._option is not None:
            self._option["label"] += data

    def handle_endtag(self, tag):
        if tag == "option":
            self._option = None
        elif tag == "select":
            self._select = None


def _selects(page: str) -> dict:
    parser = _Selects()
    parser.feed(page)
    parser.close()
    return parser.selects


def _values(select: dict) -> list:
    return [o["value"] for o in select["options"]]


def _selected(select: dict) -> list:
    return [o["value"] for o in select["options"] if "selected" in o["attrs"]]


def _etags(page: str) -> list:
    return _ETAG_INPUT.findall(page)


def _admin_lines(caplog) -> list:
    """The structured lines of ``pallas.admin_ledger``, as emitted."""
    return [r.json_fields for r in caplog.records
            if r.name == "pallas.admin_ledger" and hasattr(r, "json_fields")]


# ══════════════════════════════════════════════════════════════════════
# 1. The form: options, labels, Sens — through the real jsattr
# ══════════════════════════════════════════════════════════════════════


def test_the_create_form_offers_the_models_natures_and_the_sens(client, fake):
    """Regression — the form offered « Dépense », « Autre recette » and
    « Encaissement de facture », hard-coded: no nature, no sens."""
    resp = client.get("/administration/nouvelle")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    selects = _selects(page)

    kinds = selects["kind"]["options"]
    assert [o["value"] for o in kinds] == list(al.FORM_KINDS)
    assert {o["value"]: o["label"] for o in kinds} == {
        k: (al.INTERNAL_TRANSFER_FORM_LABEL if k == al.INTERNAL_TRANSFER_FORM_KIND
            else al.KIND_LABELS[k]) for k in al.FORM_KINDS}
    assert {o["label"] for o in kinds} >= {
        "Prélèvement de l'avocat", "Apport de l'avocat", "Virement interne"}
    assert _selected(selects["kind"]) == ["dépense"]

    sens = selects["sens_virement"]
    # A blank first: on a create the lawyer SAYS the direction — a default
    # would silently flip the money's sign.
    assert [(o["value"], o["label"]) for o in sens["options"]] == [
        ("", "Choisir le sens…"),
        *[(s, al.INTERNAL_TRANSFER_SENS_LABELS[s]) for s in al.INTERNAL_TRANSFER_KIND_BY_SENS],
    ]
    assert sens["attrs"][":disabled"] == "kind !== 'virement_interne'"
    assert sens["attrs"][":required"] == "kind === 'virement_interne'"
    assert _selected(sens) == []

    # The real jsattr: JSON strings, never a raw single-quoted interpolation.
    assert "kind: &#34;dépense&#34;," in page
    assert "sens: &#34;&#34;," in page
    assert "dossierId: &#34;&#34;," in page
    assert "kind: '" not in page and "dossierId: '" not in page

    text = html.unescape(page)
    assert ">Payeur / Fournisseur</label>" in text
    # The reminder: a card payment and a fee payment are never a virement.
    assert "« Paiement de carte »" in text
    assert "registre du fidéicommis (paiement d'honoraires)" in text


def test_the_forms_alpine_codes_are_the_models():
    """The Alpine expressions name the model's codes — pinned against
    them, so a renamed code cannot leave the form testing a dead value."""
    src = (_ATHENA / "templates" / "administration" / "form.html").read_text(
        encoding="utf-8")
    virement = al.INTERNAL_TRANSFER_FORM_KIND
    assert f"x-show=\"kind === '{virement}'\"" in src
    assert f":disabled=\"kind !== '{virement}'\"" in src
    assert f"kind === '{virement}' ? 'Compte de contrepartie'" in src
    owner = " || ".join(f"kind === '{k}'" for k in al.OWNER_KINDS)
    # The dossier's hidden input — disabled, so never posted, for the
    # lawyer's own money. The Type select's @change clears the dossier for
    # an encaissement ONLY: clearing it for a prélèvement or an apport erased
    # an edited entry's dossier when the lawyer switched back (review fix).
    assert f'name="dossier_id" :value="dossierId" :disabled="{owner}"' in src
    assert "@change=\"if (kind === 'encaissement_facture') { dossierId = ''; dossierDisplay = ''; }\"" in src
    change = src.split('<select name="kind"', 1)[1].split(">", 1)[0]
    assert all(f"'{k}'" not in change for k in al.OWNER_KINDS)
    hidden = " &amp;&amp; ".join(f"kind !== '{k}'" for k in
                                  ("encaissement_facture", *al.OWNER_KINDS))
    assert f'x-show="{hidden}"' in src
    # Disabled outside a dépense: the category and the split's fieldset —
    # NOT the supplier's invoice number (the route always sends its key: a
    # disabled, hence empty, field would erase the stored one).
    assert ('<select name="category" :disabled="kind !== \'dépense\'"' in src)
    assert '<fieldset class="min-w-0" :disabled="kind !== \'dépense\'">' in src
    line = next(l for l in src.splitlines() if 'name="supplier_invoice_ref"' in l)
    assert "disabled" not in line
    assert ('<select name="invoice_id" :disabled="kind !== \'encaissement_facture\'"'
            in src)


@pytest.mark.parametrize("stored", sorted(al.KIND_MOVES))
def test_the_edit_form_offers_exactly_the_models_passages(client, fake, stored):
    """Regression — the edit form offered « Dépense » and « Autre recette »
    whatever the kind on file. It now offers the stored kind and EXACTLY the
    passages the model permits (``KIND_MOVES``): never one the model would
    refuse (``changement_de_sens``), none it permits left out."""
    entry = _create(stored)
    resp = client.get(f"/administration/{entry['id']}/modifier")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    selects = _selects(page)
    kinds = _values(selects["kind"])
    sens = [v for v in _values(selects.get("sens_virement", {"options": []})) if v]
    offered = set()
    for value in kinds:
        if value == al.INTERNAL_TRANSFER_FORM_KIND:
            offered |= {al.INTERNAL_TRANSFER_KIND_BY_SENS[s] for s in sens}
        else:
            offered.add(value)
    assert offered == {stored, *al.KIND_MOVES[stored]}
    # In the create form's order, the stored kind selected.
    assert kinds == [k for k in al.FORM_KINDS if k in kinds]
    form_value, form_sens = al.form_kind(stored)
    assert _selected(selects["kind"]) == [form_value]
    assert f"kind: &#34;{form_value}&#34;," in page
    # The one sens a passage keeps is a fact: preselected, no blank to pick.
    if sens:
        assert len(sens) == 1
        assert _selected(selects["sens_virement"]) == sens
        assert f"sens: &#34;{sens[0]}&#34;," in page
        assert form_sens in ("", sens[0])


def test_editing_a_stored_transfer_shows_its_nature_and_sens(client, fake):
    """Regression — the form read the stored code as its own value: a
    « virement_interne_sortant » matched no option."""
    entry = _create("virement_interne_sortant", counterparty="Compte en fidéicommis",
                    dossier_id="dos1")
    page = client.get(f"/administration/{entry['id']}/modifier").get_data(as_text=True)
    selects = _selects(page)
    assert _values(selects["kind"]) == ["dépense", "prélèvement", "virement_interne"]
    assert _selected(selects["kind"]) == ["virement_interne"]
    assert [(o["value"], o["label"]) for o in selects["sens_virement"]["options"]] == [
        ("sortant", al.INTERNAL_TRANSFER_SENS_LABELS["sortant"])]
    assert _selected(selects["sens_virement"]) == ["sortant"]
    assert "kind: &#34;virement_interne&#34;," in page
    assert "sens: &#34;sortant&#34;," in page
    # A virement may carry a dossier: it is shown.
    assert "dossierId: &#34;dos1&#34;," in page
    assert "dossierDisplay: &#34;2031-001 — Dossier fictif&#34;," in page
    text = html.unescape(page)
    assert ">Compte de contrepartie</label>" in text
    assert 'placeholder="Ex. : Compte en fidéicommis"' in text
    assert _etags(page) == [fake.peek(_path(entry))["etag"]]


def test_a_refused_create_re_renders_the_chosen_nature_and_sens(client, fake):
    resp = client.post("/administration/", data=_form(
        "virement_interne", sens_virement="entrant", counterparty=""))
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert al._ABORT_MESSAGES["contrepartie_requise"] in html.unescape(page)
    selects = _selects(page)
    assert _selected(selects["kind"]) == ["virement_interne"]
    assert _selected(selects["sens_virement"]) == ["entrant"]
    assert "kind: &#34;virement_interne&#34;," in page
    assert "sens: &#34;entrant&#34;," in page
    assert ">Compte de contrepartie</label>" in html.unescape(page)
    assert _entries(fake) == {}


def test_a_refused_edit_keeps_the_nature_the_sens_and_the_submitted_etag(client, fake):
    entry = _create("recette_autre", counterparty="Compte en fidéicommis")
    before = fake.peek(_path(entry))
    resp = client.post(f"/administration/{entry['id']}/modifier", data={
        **_form("virement_interne", sens_virement="entrant", counterparty=""),
        "expected_etag": before["etag"]})
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert al._ABORT_MESSAGES["contrepartie_requise"] in html.unescape(page)
    assert "kind: &#34;virement_interne&#34;," in page
    assert "sens: &#34;entrant&#34;," in page
    assert _etags(page) == [before["etag"]]          # the original protects the retry
    assert fake.peek(_path(entry)) == before
    # The passages of the kind on file (an autre recette), not of the
    # virement the refused POST named.
    selects = _selects(page)
    assert _values(selects["kind"]) == [
        "dépense", "recette_autre", "apport", "virement_interne"]
    assert _selected(selects["kind"]) == ["virement_interne"]
    assert _selected(selects["sens_virement"]) == ["entrant"]


def test_the_x_data_never_echoes_a_posted_value_raw(client, fake):
    """Regression — ``kind`` and ``dossierId`` were interpolated raw into
    single-quoted JS strings: a refusal (400) re-renders what the form
    POSTED, and the attribute is decoded of its entities BEFORE Alpine
    evaluates it — under 'unsafe-eval', an expression injection."""
    hostile = "x');alert(1);('"
    resp = client.post("/administration/", data=_form(hostile, category="loyer"))
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    # A kind the form never offered never reaches the select.
    assert "kind: &#34;dépense&#34;," in page
    assert "alert(1)" not in page.split("x-init", 1)[0].split("x-data", 1)[1]

    resp = client.post("/administration/", data=_form(
        "dépense", category="loyer", dossier_id="inconnu" + hostile))
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert "dossierId: &#34;inconnux&#39;);alert(1);(&#39;&#34;," in page
    assert "dossierId: '" not in page and "kind: '" not in page
    assert _entries(fake) == {}


# ══════════════════════════════════════════════════════════════════════
# 2. The refusals: a missing sens, and the stale hidden fields
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("sens", [None, "", "nord"])
def test_a_transfer_without_its_sens_is_refused_and_nothing_written(
    client, fake, caplog, sens,
):
    """Regression — without the select's sens, the form's own
    « virement_interne » reached the model, which answered « Le type
    d'opération est invalide »: the wrong field, and no line naming it."""
    caplog.set_level(logging.INFO, logger="pallas.admin_ledger")
    form = _form("virement_interne")
    if sens is not None:
        form["sens_virement"] = sens
    fake.reset_logs()
    resp = client.post("/administration/", data=form)
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert SENS_REQUIS in page
    assert al._ABORT_MESSAGES["type_invalide"] not in html.unescape(page)
    assert _entries(fake) == {}
    assert fake.commits == []
    assert _admin_lines(caplog) == [{
        "event": "admin_transaction_refused", "outcome": "refused",
        "account_id": "ops1", "reason": "sens_requis",
    }]
    # The re-render keeps the choice, its sens blank.
    assert _selected(_selects(page)["kind"]) == ["virement_interne"]
    assert "sens: &#34;&#34;," in page


def test_an_edit_without_the_sens_is_refused_with_the_submitted_etag(
    client, fake, caplog,
):
    entry = _create("dépense")
    before = fake.peek(_path(entry))
    caplog.set_level(logging.INFO, logger="pallas.admin_ledger")
    resp = client.post(f"/administration/{entry['id']}/modifier", data={
        **_form("virement_interne", sens_virement="entrant-ish"),
        "expected_etag": before["etag"]})
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert SENS_REQUIS in page
    assert fake.peek(_path(entry)) == before
    assert _etags(page) == [before["etag"]]
    assert _admin_lines(caplog) == [{
        "event": "admin_transaction_refused", "outcome": "refused",
        "transaction_id": entry["id"], "account_id": "ops1",
        "reason": "sens_requis",
    }]
    # The re-render offers the passages of the kind ON FILE — the next save
    # is judged against it — never those of the value just refused.
    selects = _selects(page)
    assert _values(selects["kind"]) == [
        "dépense", "recette_autre", "prélèvement", "virement_interne"]
    assert _selected(selects["kind"]) == ["virement_interne"]
    assert _values(selects["sens_virement"]) == ["sortant"]
    assert _selected(selects["sens_virement"]) == ["sortant"]


def test_stale_hidden_fields_cause_no_refusal(client, fake):
    """Regression — a field the form hides for a nature (the category, the
    split, the invoice, a prélèvement's dossier) is still posted until
    Alpine disables it, and the model refuses it (it judges what the write
    CARRIES). The route neutralizes it, its key kept."""
    stale = {"category": "loyer", "net_amount": "80,00", "gst_amount": "5,00",
             "qst_amount": "15,00", "invoice_id": "facture-perimee",
             "dossier_id": "dos1", "supplier_invoice_ref": "F-9"}
    # The model's own answer to those fields — what the neutralization spares.
    for field, value, reason in (
        ("category", "loyer", "catégorie_interdite"),
        ("net_amount", 8000, "ventilation_interdite"),
        ("invoice_id", "facture-perimee", "facture_interdite"),
        ("dossier_id", "dos1", "dossier_interdit"),
    ):
        _, errs = al.create_transaction(_data("prélèvement", **{field: value}))
        assert errs == [al._ABORT_MESSAGES[reason]], field
    assert _entries(fake) == {}

    resp = client.post("/administration/", data=_form("prélèvement", **stale))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    (stored,) = _entries(fake).values()
    assert stored["kind"] == "prélèvement" and stored["direction"] == "déboursé"
    assert stored["category"] is None
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        10000, 0, 0)
    assert stored["invoice_id"] is None
    assert stored["dossier_id"] is None and stored["dossier_file_number"] == ""
    assert stored["supplier_invoice_ref"] == "F-9"   # never hidden-and-erased

    # A virement interne keeps its dossier — only the lawyer's own money
    # never carries one.
    resp = client.post("/administration/", data=_form(
        "virement_interne", sens_virement="entrant", **stale))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    (virement,) = [e for e in _entries(fake).values() if e["id"] != stored["id"]]
    assert virement["kind"] == "virement_interne_entrant"
    assert virement["direction"] == "recette"
    assert virement["category"] is None and virement["invoice_id"] is None
    assert (virement["net_amount"], virement["gst_amount"], virement["qst_amount"]) == (
        0, 0, 0)
    assert virement["dossier_id"] == "dos1"
    assert virement["dossier_file_number"] == "2031-001"
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 0  # −100 + 100


def test_the_form_turns_a_depense_into_a_drawing(client, fake):
    """The edit form posts every field of the dépense it showed — its
    category, split and dossier still in the hidden block. The model alone
    refuses that change (the dossier the entry carries); the route clears
    what a prélèvement cannot carry and the passage goes through."""
    dep = _create("dépense", amount=11498, net_amount=10000, gst_amount=500,
                  qst_amount=998, dossier_id="dos1", supplier_invoice_ref="F-1")
    _, errs = al.update_transaction(dep["id"], {"kind": "prélèvement",
                                                "dossier_id": "dos1"})
    assert errs == [al._ABORT_MESSAGES["dossier_interdit"]]
    before = fake.peek(_path(dep))
    resp = client.post(f"/administration/{dep['id']}/modifier", data={
        **_form("prélèvement", amount="114,98", category="loyer",
                net_amount="100,00", gst_amount="5,00", qst_amount="9,98",
                dossier_id="dos1", supplier_invoice_ref="F-1"),
        "expected_etag": before["etag"]})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    stored = fake.peek(_path(dep))
    assert stored["kind"] == "prélèvement" and stored["direction"] == "déboursé"
    assert stored["category"] is None
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        11498, 0, 0)
    assert stored["dossier_id"] is None and stored["dossier_file_number"] == ""
    assert stored["supplier_invoice_ref"] == "F-1"
    assert set(stored["revisions"][-1]["changes"]) >= {
        "kind", "category", "dossier_id", "net_amount", "gst_amount", "qst_amount"}


def test_the_form_turns_a_depense_into_an_outgoing_transfer(client, fake):
    dep = _create("dépense", dossier_id="dos1")
    resp = client.post(f"/administration/{dep['id']}/modifier", data={
        **_form("virement_interne", sens_virement="sortant", category="loyer",
                dossier_id="dos1"),
        "expected_etag": fake.peek(_path(dep))["etag"]})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    stored = fake.peek(_path(dep))
    assert stored["kind"] == "virement_interne_sortant"
    assert stored["direction"] == "déboursé"
    assert stored["category"] is None
    assert (stored["net_amount"], stored["gst_amount"], stored["qst_amount"]) == (
        10000, 0, 0)
    assert stored["dossier_id"] == "dos1"


# ══════════════════════════════════════════════════════════════════════
# 3. What the pages say
# ══════════════════════════════════════════════════════════════════════


_SPAN = re.compile(r'<span class="([^"]*rounded-full[^"]*)">([^<]*)</span>')


def test_each_nature_shows_as_plain_text(client, fake):
    """The lawyer's decision (2026-10-07): the four natures read as plain
    text in the journal and on the fiche, like every other kind — no pill.
    Regression: they wore a purple or teal chip."""
    ids = {}
    for i, kind in enumerate(NATURES):
        ids[kind] = _create(kind, counterparty=f"Ligne {i}")["id"]
    _create("dépense", counterparty="Ligne dépense")
    page = html.unescape(client.get("/administration/?account_id=ops1").get_data(as_text=True))
    assert "kind_badges" not in ra._labels()
    for kind in (*NATURES, "dépense"):
        cell = f'<td class="px-3 py-2 text-gray-500">{al.KIND_LABELS[kind]}</td>'
        assert cell in page, kind
    assert not [t for _c, t in _SPAN.findall(page) if t in al.KIND_LABELS.values()]
    for kind in NATURES:
        detail = html.unescape(
            client.get(f"/administration/{ids[kind]}").get_data(as_text=True))
        assert f'<p class="text-gray-900">{al.KIND_LABELS[kind]}</p>' in detail, kind
        assert not [t for _c, t in _SPAN.findall(detail) if t in al.KIND_LABELS.values()], kind


def _avoir(page: str):
    """The « Avoir de l'avocat » block of an (unescaped) page:
    ``{"period", "Apports", "Prélèvements", "Solde net"}``, or ``None``."""
    head = re.search(r"Avoir de l'avocat — ([^<]*)</p>", page)
    if head is None:
        return None
    figures = dict(re.findall(
        r'<p class="text-xs text-gray-500">(Apports|Prélèvements|Solde net)</p>\s*'
        r'<p class="text-lg font-semibold text-gray-900">([^<]*)</p>',
        page[head.end():]))
    return {"period": head.group(1), **figures}


def _seed_avoir() -> None:
    _create("apport", amount=100000, counterparty="Apport", date=_d(2031, 9, 2))
    _create("prélèvement", amount=25000, counterparty="Retrait", date=_d(2031, 9, 3))
    _create("apport", amount=40000, counterparty="Apport ancien", date=_d(2031, 8, 15))
    _create("dépense", amount=5000, counterparty="Dépense", date=_d(2031, 9, 4))
    # A virement is neither an apport nor a prélèvement.
    _create("virement_interne_entrant", amount=70000, counterparty="Virement",
            date=_d(2031, 9, 5))


def _get(client, path: str, **headers) -> str:
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.status_code
    return html.unescape(resp.get_data(as_text=True))


def test_the_journal_shows_no_owner_equity_card(client, fake):
    """The lawyer's decision (2026-10-07): the journal header carries no
    « Avoir de l'avocat » card — the account page (the calendar year) and
    the PDF journal keep it. Regression: the header showed it for the
    period (« Depuis l'ouverture du compte » with no dates), also in the
    out-of-band header a filter re-emits."""
    _seed_avoir()
    for path in ("/administration/?account_id=ops1",
                 "/administration/?account_id=ops1&date_from=2031-09-01&date_to=2031-09-30"):
        assert "Avoir de l'avocat" not in _get(client, path), path
        fragment = _get(client, path, **{"HX-Request": "true"})
        assert '<div id="admin-header" hx-swap-oob="true">' in fragment
        assert "Avoir de l'avocat" not in fragment, path
    # The account page still shows the year's figures.
    assert _avoir(_get(client, "/administration/comptes/ops1")) is not None


def _truncating(monkeypatch) -> None:
    """The register read returns its rows, flagged truncated."""
    real = al.list_register

    def _read(account_id, date_from=None, date_to=None, limit=10000):
        rows, _truncated = real(account_id, date_from, date_to, limit)
        return rows, True

    monkeypatch.setattr(ra.al, "list_register", _read)


def _failing(monkeypatch) -> None:
    def _read(*_a, **_k):
        raise RuntimeError("registre illisible")

    monkeypatch.setattr(ra.al, "list_register", _read)


def test_the_account_page_shows_the_calendar_years_owner_equity(client, fake, monkeypatch):
    """Regression — the account page said nothing of it. The calendar year
    to date: from January 1 to Montréal's today, frozen here at 2031-09-20."""
    _create("apport", amount=40000, counterparty="Apport", date=_d(2030, 12, 31))
    _create("apport", amount=100000, counterparty="Apport", date=_d(2031, 1, 1))
    _create("prélèvement", amount=25000, counterparty="Retrait", date=_d(2031, 9, 20))
    reads = []
    real = al.list_register

    def _spy(account_id, date_from=None, date_to=None, limit=10000):
        reads.append((account_id, date_from, date_to))
        return real(account_id, date_from, date_to, limit)

    monkeypatch.setattr(ra.al, "list_register", _spy)
    page = _get(client, "/administration/comptes/ops1")
    assert reads == [("ops1", _d(2031, 1, 1), _d(2031, 9, 20))]
    assert _avoir(page) == {
        "period": "Période du 2031-01-01 au 2031-09-20", "Apports": _fmt(100000),
        "Prélèvements": _fmt(25000), "Solde net": _fmt(75000)}


def test_the_account_page_says_unavailable_never_zero(client, fake, monkeypatch, caplog):
    _create("apport", amount=100000, counterparty="Apport")
    _failing(monkeypatch)
    page = _get(client, "/administration/comptes/ops1")
    assert "Avoir de l'avocat — Période du 2031-01-01 au 2031-09-20" in page
    assert "Avoir indisponible" in page
    assert _avoir(page) == {"period": "Période du 2031-01-01 au 2031-09-20"}  # no figure
    assert "Solde net" not in page
    assert any(getattr(r, "json_fields", {}).get("event") == "unexpected"
               for r in caplog.records)


def test_the_account_page_shows_nothing_over_a_truncated_read(client, fake, monkeypatch):
    _create("apport", amount=100000, counterparty="Apport")
    _truncating(monkeypatch)
    page = _get(client, "/administration/comptes/ops1")
    assert "Avoir de l'avocat" not in page
    assert "indisponible" not in page


def test_the_csv_carries_each_lines_nature(client, fake):
    """Regression — the CSV had no kind column: a prélèvement, which has no
    category, was an anonymous déboursé there."""
    kinds = ("dépense", "recette_autre", *NATURES)
    for i, kind in enumerate(kinds):
        _create(kind, counterparty=f"Ligne {i}", date=_d(2031, 9, 1 + i))
    resp = client.get("/administration/export/csv?account_id=ops1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).lstrip("\N{ZERO WIDTH NO-BREAK SPACE}")
    rows = list(csv.reader(io.StringIO(body)))
    header = rows[0]
    assert header[:4] == ["Date", "Fournisseur / Source", "Nature", "Catégorie"]
    by_line = {r[1]: dict(zip(header, r)) for r in rows[1:]}
    for i, kind in enumerate(kinds):
        row = by_line[f"Ligne {i}"]
        assert row["Nature"] == al.KIND_LABELS[kind], kind
        assert row["Catégorie"] == ("Loyer" if kind == "dépense" else ""), kind
        if kind in NATURES:
            # The canonical split is never printed as a claimable net.
            assert (row["Net"], row["TPS"], row["TVQ"]) == ("", "", ""), kind


def _plain(text: str) -> str:
    for space in _SPACES:
        text = text.replace(space, " ")
    return text


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return _plain("\n".join(p.extract_text() or ""
                            for p in PdfReader(io.BytesIO(data)).pages))


def _money(cents: int) -> str:
    return _plain(_fmt(cents))


@pytest.fixture
def pdf_calls(monkeypatch) -> list:
    """Every call the route makes to the sheet builder — the real builder
    still renders it."""
    calls: list = []
    real = ajp.build_admin_journal_pdf

    def _spy(rows, **kw):
        calls.append({"rows": rows, **kw})
        return real(rows, **kw)

    monkeypatch.setattr(ajp, "build_admin_journal_pdf", _spy)
    return calls


def _seed_pdf() -> None:
    _create("apport", amount=100000, counterparty="Ligne A", date=_d(2031, 9, 2))
    _create("prélèvement", amount=25000, counterparty="Ligne P", date=_d(2031, 9, 3))
    _create("virement_interne_sortant", amount=30000, counterparty="Ligne V",
            date=_d(2031, 9, 4))
    _create("dépense", amount=11498, net_amount=10000, gst_amount=500,
            qst_amount=998, counterparty="Ligne D", date=_d(2031, 9, 5))


def test_the_pdf_names_the_nature_and_prints_the_owner_equity(client, fake, pdf_calls):
    """Regression — the sheet has no « Type » column (its columns are
    pinned): a prélèvement was a déboursé with no category there. Its empty
    « Catégorie » cell now names its nature, and an avoir line follows the
    tax line — which counts the dépense alone."""
    _seed_pdf()
    resp = client.get("/administration/export/pdf?account_id=ops1")
    assert resp.status_code == 200 and resp.data.startswith(b"%PDF")
    (call,) = pdf_calls
    by_line = {r["counterparty"]: r for r in call["rows"]}
    assert by_line["Ligne A"]["categorie"] == "Apport de l'avocat"
    assert by_line["Ligne P"]["categorie"] == "Prélèvement de l'avocat"
    assert by_line["Ligne V"]["categorie"] == "Virement interne (sortant)"
    assert by_line["Ligne D"]["categorie"] == "Loyer"
    assert call["avoir"] == {"contributions": 100000, "drawings": 25000, "net": 75000}
    assert (call["tps_total"], call["tvq_total"]) == (500, 998)

    text = _pdf_text(resp.data)
    for label in ("Apport de l'avocat", "Prélèvement de l'avocat",
                  "Virement interne (sortant)"):
        assert label in text, label
    tax = text.index(f"TPS : {_money(500)} · TVQ : {_money(998)}")
    avoir = text.index(
        f"Avoir de l'avocat pour la période — Apports : {_money(100000)} · "
        f"Prélèvements : {_money(25000)} · Solde net : {_money(75000)}")
    assert tax < avoir
    # The Recette/Déboursé totals stay inclusive of every row.
    assert _money(100000) in text and _money(66498) in text   # 250 + 300 + 114,98


@pytest.mark.parametrize("read", ["truncated", "failed"])
def test_the_pdf_omits_the_owner_equity_of_an_incomplete_read(
    client, fake, monkeypatch, pdf_calls, read,
):
    _seed_pdf()
    (_truncating if read == "truncated" else _failing)(monkeypatch)
    resp = client.get("/administration/export/pdf?account_id=ops1")
    assert resp.status_code == 200 and resp.data.startswith(b"%PDF")
    (call,) = pdf_calls
    assert call["avoir"] is None                    # never a zero for an unknown
    text = _pdf_text(resp.data)
    assert "Avoir de l'avocat" not in text
    notice = ("le registre a été tronqué" if read == "truncated"
              else "n'ont pas pu être lues")
    assert notice in text


def test_the_sheet_prints_the_owner_equity_after_the_taxes_only_when_given():
    """The builder's own contract — its pinned columns untouched."""
    row = {"date": "2031-09-03", "counterparty": "Ligne P",
           "categorie": "Prélèvement de l'avocat", "facture": "", "mode": "Virement",
           "net": None, "tps": None, "tvq": None, "recette": None,
           "debours": 25000, "solde": -25000, "en_circulation": False}
    common = dict(account_line="Opérations", period="Période fictive",
                  filename="t.pdf", tps_total=0, tvq_total=0)
    with_avoir = _pdf_text(ajp.build_admin_journal_pdf(
        [row], avoir={"contributions": 0, "drawings": 25000, "net": -25000},
        **common).data)
    assert with_avoir.index("Taxes payées sur les déboursés de la période") < \
        with_avoir.index(f"Avoir de l'avocat pour la période — Apports : {_money(0)} · "
                         f"Prélèvements : {_money(25000)} · Solde net : {_money(-25000)}")
    without = _pdf_text(ajp.build_admin_journal_pdf([row], **common).data)
    assert "Avoir de l'avocat" not in without
    assert [c.key for c in ajp.COLUMNS] == [
        "date", "counterparty", "categorie", "facture", "mode",
        "net", "tps", "tvq", "recette", "debours", "solde"]


def test_the_fiche_shows_a_revisions_motif_and_par_script(client, fake):
    """Regression — the fiche listed a revision's fields and « par
    Claude », never its motif nor « par script »: a reclassification made
    outside the model left only field names there."""
    entry = _create("prélèvement", counterparty="Retrait")
    doc = fake.peek(_path(entry))
    motif = "Reclassement : vérification du journal du 2031-09-12, ligne 7"
    doc["revisions"] = [
        {"at": datetime(2031, 9, 10, 14, 0, tzinfo=UTC), "via": "web",
         "changes": {"description": ["", "Retrait de septembre"]}},
        {"at": datetime(2031, 9, 12, 14, 0, tzinfo=UTC), "via": "script",
         "motif": motif,
         "changes": {"kind": ["dépense", "prélèvement"], "category": ["loyer", None]}},
    ]
    fake.external_write(_path(entry), doc)
    page = _get(client, f"/administration/{entry['id']}")
    revisions = page[page.index("Révisions (2)"):]
    revisions = revisions[:revisions.index("</details>")]
    script, web = re.findall(r"<li>(.*?)</li>", revisions, re.S)   # newest first
    assert script.startswith("2031-09-12 10:00 — ")
    assert script.endswith(f" · par script — {motif}")
    assert {"kind", "category"} == set(
        script.split(" — ")[1].split(" · ")[0].split(", "))
    assert web == "2031-09-10 10:00 — description"


# ══════════════════════════════════════════════════════════════════════
# 4. Compiled classes only
# ══════════════════════════════════════════════════════════════════════


def _block(page: str, marker: str, last: str) -> str:
    """From the ``<div>`` opening the block that holds *marker* to the
    element that holds *last*."""
    i = page.index(marker)
    start = page.rindex("<div", 0, i)
    end = page.index("</div>", page.index(last, i))
    return page[start:end]


def _absent_classes(classes: set) -> list:
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    return absent


def test_the_new_markup_uses_only_compiled_classes(client, fake, monkeypatch):
    """A class absent from the compiled artifact silently does not apply,
    and adding one is a seven-file fan-out (CLAUDE.md item 6)."""
    _seed_avoir()
    ids = {kind: _create(kind, counterparty=f"Pastille {kind}")["id"] for kind in NATURES}
    journal = _get(client, "/administration/?account_id=ops1")
    account = _get(client, "/administration/comptes/ops1")
    form = _get(client, "/administration/nouvelle")
    detail = _get(client, f"/administration/{ids['prélèvement']}")
    snippets = [
        _block(account, "Avoir de l'avocat", "Solde net"),
        _block(form, 'name="sens_virement"', "« Paiement de carte »"),
        form[form.index("<fieldset"):form.index(">", form.index("<fieldset")) + 1],
    ]
    _failing(monkeypatch)
    unavailable = _get(client, "/administration/comptes/ops1")
    snippets.append(_block(unavailable, "Avoir de l'avocat", "Avoir indisponible"))

    classes = {c for s in snippets
               for block in re.findall(r'class="([^"]+)"', s)
               for c in block.split()}
    assert {"grid-cols-2", "sm:grid-cols-3", "text-lg", "min-w-0",
            "hover:underline"} <= classes
    assert not _absent_classes(classes), _absent_classes(classes)
