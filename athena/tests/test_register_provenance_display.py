"""La fiche d'une écriture dit ce que le connecteur y a fait (lot 5).

L'écran de consentement de la comptabilité promet : « Chaque écriture est
marquée comme provenant de Claude ». La MARQUE était stockée —
``created_via`` / ``cleared_via`` sur la ligne, ``via`` sur chaque révision
d'une écriture d'administration — mais aucune page de l'application ne la
montrait : seul ``get_admin_ledger`` la rendait, au connecteur lui-même.
Or le juriste relit les écritures que Claude a inscrites ou compensées, et
une écriture de registre ne s'efface pas (elle se contre-passe) : c'est sur
la fiche qu'il doit pouvoir la reconnaître, comme une facture porte
« créée par Claude » depuis le lot 3.

Chaque test échoue sur les gabarits antérieurs (revue de complétude du
lot 5). Le banc est celui du connecteur comptable (le faux Firestore
partagé, les vrais gestionnaires, sous ``writing_via("mcp")`` comme au
``tools/call``), et les pages sont rendues par les vraies routes.
"""

import html
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

from tests.test_mcp_accounting import (  # noqa: E402,F401 — the bench and its fixture
    _call, _deposit, _depense, fake,
)

with mock.patch("google.cloud.firestore.Client"):
    from models import provenance
    from services import comptabilite as svc
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests.test_kyc_rendering import _escape_css  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
CREATED = "inscrite par Claude"
CLEARED = "compensée par Claude"

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
        jsattr=lambda v: v,
    )
    for bp in (trust_routes.trust_bp, invoices_routes.invoices_bp,
               admin_ledger_routes.admin_bp, dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _by_claude(tool: str, **args) -> dict:
    """A tool call as ``tools/call`` makes it — its writes stamped « mcp »."""
    with provenance.writing_via("mcp", tool=tool):
        return _call(tool, **args)


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _web_deposit() -> dict:
    """A deposit the lawyer records on the trust form — the SAME service
    door the route uses (a handler would stamp « mcp » whatever the
    caller: ``run_write`` declares its writer)."""
    with provenance.writing_via("web"):
        report = svc.enregistrer_ecriture_fideicommis({
            "account_id": "acc1", "direction": "recette", "purpose": "dépôt_client",
            "amount": 200000, "date": _d(2026, 9, 2), "method": "chèque",
            "dossier_id": "dos1", "client_id": "c1", "counterparty": "Jean Tremblay",
            "reference": "", "description": "",
        })
    assert report["ok"], report
    return report["entry"]


def _web_depense() -> dict:
    with provenance.writing_via("web"):
        report = svc.enregistrer_ecriture_administration({
            "account_id": "ops1", "kind": "dépense", "direction": "déboursé",
            "amount": 11498, "date": _d(2026, 9, 5), "method": "virement",
            "category": "loyer", "counterparty": "Immeubles X",
            "net_amount": 11498, "gst_amount": 0, "qst_amount": 0,
            "reference": "", "description": "",
        })
    assert report["ok"], report
    return report["entry"]


def _page(client, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, resp.status_code
    return html.unescape(resp.get_data(as_text=True))


def _header(page: str) -> str:
    """The title row of the fiche — where the status chip sits."""
    start = page.index("<h1")
    return page[start:page.index("</div>", start)]


def test_a_trust_entry_claude_recorded_and_cleared_says_so(client, fake):
    rec = _by_claude("record_trust_entry", **_deposit())
    tx_id = rec["entity"]["id"]
    header = _header(_page(client, f"/fideicommis/{tx_id}"))
    assert CREATED in header and CLEARED not in header

    _by_claude("clear_register_entries", register="trust", tx_ids=[tx_id],
               cleared_date="2026-09-03")
    header = _header(_page(client, f"/fideicommis/{tx_id}"))
    assert CREATED in header and CLEARED in header


def test_a_trust_entry_the_lawyer_wrote_carries_no_mark(client, fake):
    tx_id = _web_deposit()["id"]
    # Claude clears it: only the clearing is his.
    _by_claude("clear_register_entries", register="trust", tx_ids=[tx_id],
               cleared_date="2026-09-03")
    header = _header(_page(client, f"/fideicommis/{tx_id}"))
    assert CREATED not in header and CLEARED in header


def test_an_administration_entry_says_who_wrote_and_who_corrected_it(client, fake):
    dep = _by_claude("record_admin_entry", **_depense())
    tx_id = dep["entity"]["id"]
    page = _page(client, f"/administration/{tx_id}")
    assert CREATED in _header(page)
    assert "Révisions (" not in page            # nothing corrected yet

    _by_claude("update_admin_entry", tx_id=tx_id,
               expected_etag=dep["entity"]["etag"], description="Loyer de septembre")
    page = _page(client, f"/administration/{tx_id}")
    revisions = page[page.index("Révisions ("):]
    revisions = revisions[:revisions.index("</details>")]
    assert re.search(r"description · par Claude", revisions), revisions


def test_an_administration_entry_the_lawyer_wrote_and_corrected_carries_no_mark(
    client, fake,
):
    dep = _web_depense()
    tx_id = dep["id"]
    with provenance.writing_via("web"):
        report = svc.modifier_ecriture_administration(
            tx_id, {"description": "Loyer"}, expected_etag=dep["etag"])
    assert report["ok"], report
    page = _page(client, f"/administration/{tx_id}")
    assert "Révisions (1)" in page              # the correction IS listed
    assert CREATED not in page and CLEARED not in page
    assert "par Claude" not in page


def test_the_marks_use_only_compiled_classes(client, fake):
    """A class absent from the compiled artifact silently does not apply,
    and adding one is a seven-file fan-out (CLAUDE.md item 6)."""
    rec = _by_claude("record_trust_entry", **_deposit())
    _by_claude("clear_register_entries", register="trust",
               tx_ids=[rec["entity"]["id"]], cleared_date="2026-09-03")
    dep = _by_claude("record_admin_entry", **_depense())
    snippets = [
        _header(_page(client, f"/fideicommis/{rec['entity']['id']}")),
        _header(_page(client, f"/administration/{dep['entity']['id']}")),
    ]
    # The header row itself (its flex-wrap) and the chips inside it.
    for path in (f"/fideicommis/{rec['entity']['id']}",
                 f"/administration/{dep['entity']['id']}"):
        page = _page(client, path)
        start = page.rindex("<div", 0, page.index("<h1"))
        snippets.append(page[start:page.index(">", start) + 1])
    classes = {c for s in snippets
               for block in re.findall(r'class="([^"]+)"', s)
               for c in block.split()}
    assert "flex-wrap" in classes and "bg-gray-200" in classes
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert not absent, absent
