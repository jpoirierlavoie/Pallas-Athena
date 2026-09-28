"""Émettre une facture : le plan unique et les règles d'émission (lot 3a).

``models.invoice.plan_invoice`` porte TOUT ce que ``create_invoice`` décide
avant sa transaction — la sélection des sources, les refus, les totaux, les
contrôles d'émission, les avertissements — pour que l'aperçu du connecteur
(lot 3b) et l'écriture ne puissent pas diverger. Et quatre défauts
silencieux du chemin GÉNÉRÉ (la facture que l'application émet aujourd'hui),
chacun atteignable depuis le formulaire web :

1. une entrée de temps NON FACTURABLE devenait une ligne à 0 $ dont les
   heures s'imprimaient sur la note du client ;
2. un dossier sans client, ou dont le client ne se résout plus, donnait une
   facture sans destinataire et une adresse figée VIDE ;
3. une facture portant la TPS/TVQ s'émettait sous des numéros d'inscription
   VIDES (CLAUDE.md en recense plus de cinquante) ;
4. une provision (``retainer_applied``) se déduisait à la création, alors
   qu'elle s'impute après l'envoi par un « paiement d'honoraires » du
   fidéicommis : le même argent compté deux fois.

Le formulaire web passe aussi ``require_all_sources`` depuis ce lot : une
sélection périmée est REFUSÉE par son nom au lieu de produire en silence une
facture plus courte que la page.

Tout passe par le faux Firestore partagé (``tests/_fake_firestore.py`` — le
client, ses transactions et la boucle de reprise de ``transactional`` sont
les vrais) ; on relit ce qui est STOCKÉ.
"""

import ast
import json
import os
import pathlib
import re
import sys
from datetime import date, datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import expense as expense_model  # noqa: F401
    from models import invoice as invoice_model
    from models import settings as settings_model  # noqa: F401
    from models import time_entry as time_entry_model  # noqa: F401
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.parties as parties_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
TODAY = date(2026, 6, 15)
WHEN = datetime(2026, 6, 15, tzinfo=UTC)
COUNTER = "counters/invoices-2026"
GST = "123456789 RT0001"
QST = "1234567890 TQ0001"


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    # Millésime FIGÉ — jamais un offset dérivé de l'horloge.
    monkeypatch.setattr(invoice_model, "today_mtl", lambda: TODAY)
    f.seed("parties/p1", {"id": "p1", "type": "individual",
                          "first_name": "Jean", "last_name": "Tremblay",
                          "work_address_street": "1 rue X",
                          "work_address_city": "Montréal"})
    return f


def _entry(fake, eid: str, **over) -> str:
    doc = {"id": eid, "dossier_id": "d1", "date": WHEN,
           "description": "Rédaction", "hours": 1.0, "rate": 30000,
           "amount": 30000, "billable": True, "invoiced": False,
           "invoice_id": None, "etag": f"etag-{eid}"}
    doc.update(over)
    fake.seed(f"timeentries/{eid}", doc)
    return eid


def _expense(fake, xid: str, **over) -> str:
    doc = {"id": xid, "dossier_id": "d1", "date": WHEN,
           "description": "Timbre", "amount": 5000, "taxable": True,
           "invoiced": False, "invoice_id": None, "etag": f"etag-{xid}"}
    doc.update(over)
    fake.seed(f"expenses/{xid}", doc)
    return xid


def _data(**over) -> dict:
    """What the web form sends for the dossier's first client."""
    base = {
        "dossier_id": "d1", "date": WHEN,
        "client_id": "p1", "client_name": "Jean Tremblay",
        "billing_address": {"name": "Jean Tremblay", "street": "1 rue X",
                            "unit": "", "city": "Montréal",
                            "province": "QC", "postal_code": ""},
        "gst_number": GST, "qst_number": QST,
    }
    base.update(over)
    return base


def _nothing_written(fake, *source_paths: str) -> None:
    assert fake.peek_collection("invoices") == {}
    assert fake.peek(COUNTER) is None, "a refusal must never touch the counter"
    for path in source_paths:
        assert fake.peek(path)["invoiced"] is False


# ══════════════════════════════════════════════════════════════════════
# 1. Le temps non facturable ne se facture jamais sur le chemin généré
# ══════════════════════════════════════════════════════════════════════


def test_une_entree_non_facturable_est_refusee_par_son_nom(fake):
    """(Régression : l'ancien create_invoice la retenait comme une ligne à
    0 $ — ses heures s'imprimaient sur la note du client.)"""
    ok = _entry(fake, "te-ok")
    nf = _entry(fake, "te-nf", billable=False, amount=0)
    doc, errors = invoice_model.create_invoice(
        "d1", [ok, nf], [], _data(), require_all_sources=True)
    assert doc is None
    assert "non facturable : te-nf" in errors[0]
    assert "Sources inutilisables" in errors[0]
    _nothing_written(fake, "timeentries/te-ok", "timeentries/te-nf")


def test_sans_le_drapeau_elle_est_ecartee_jamais_facturee(fake):
    ok = _entry(fake, "te-ok")
    _entry(fake, "te-nf", billable=False, amount=0)
    doc, errors = invoice_model.create_invoice("d1", [ok, "te-nf"], [], _data())
    assert errors == [], errors
    items = fake.peek_collection(f"invoices/{doc['id']}/lineitems")
    assert [i["source_id"] for i in items.values()] == ["te-ok"]
    assert fake.peek("timeentries/te-nf")["invoiced"] is False


def test_une_entree_heritee_sans_la_cle_reste_facturable(fake):
    """Le test est un `is False` explicite : une entrée antérieure au
    drapeau (aucune clé) n'est pas « non facturable »."""
    eid = _entry(fake, "te-old")
    stored = fake.peek(f"timeentries/{eid}")
    del stored["billable"]
    fake.seed(f"timeentries/{eid}", stored)
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(), require_all_sources=True)
    assert errors == [], errors
    assert fake.peek(f"timeentries/{eid}")["invoiced"] is True


def test_l_import_reproduit_une_ligne_non_facturable(fake):
    """Une facture historique a pu porter une ligne à 0 $ : l'import la
    reproduit, il ne réémet rien."""
    nf = _entry(fake, "te-nf", billable=False, amount=0)
    doc, errors = invoice_model.create_invoice(
        "d1", [nf], [], _data(), invoice_number="2019-F007",
        require_all_sources=True)
    assert errors == [], errors
    assert fake.peek("timeentries/te-nf")["invoiced"] is True


def test_une_source_deja_facturee_nomme_sa_facture(fake):
    eid = _entry(fake, "te-b", invoiced=True, invoice_id="inv-autre")
    xid = _expense(fake, "ex-b", invoiced=True, invoice_id="inv-autre")
    plan = invoice_model.plan_invoice(
        "d1", [eid], [xid], invoice_model.invoice_document_from(_data()),
        generated=True, require_all_sources=False)
    assert plan.skipped == {"te-b": "déjà facturée (facture inv-autre)",
                            "ex-b": "déjà facturé (facture inv-autre)"}


# ══════════════════════════════════════════════════════════════════════
# 2. Le client : sur fiche, ou pas de facture
# ══════════════════════════════════════════════════════════════════════


def test_un_dossier_sans_client_ne_recoit_pas_de_facture(fake):
    """(Régression : l'ancien chemin web émettait une facture sans
    destinataire, à l'adresse figée vide.)"""
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(client_id="", client_name="",
                               billing_address={}))
    assert doc is None
    assert any("aucun client" in e for e in errors), errors
    _nothing_written(fake, "timeentries/te1")


@pytest.mark.parametrize("number", [None, "2019-F007"], ids=["genere", "import"])
def test_un_client_introuvable_est_refuse_sur_les_deux_chemins(fake, number):
    eid = _entry(fake, "te1")
    kw = {"invoice_number": number} if number else {}
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(client_id="p-disparu"), **kw)
    assert doc is None
    assert any("client du dossier est introuvable" in e for e in errors), errors
    _nothing_written(fake, "timeentries/te1")


def test_l_import_d_un_dossier_sans_client_reste_permis(fake):
    """Seul un dossier VRAIMENT sans client passe, et seulement à l'import :
    l'ancien système a émis cette facture, on la reproduit."""
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], {"dossier_id": "d1", "date": WHEN},
        invoice_number="2019-F007")
    assert errors == [], errors
    assert fake.peek(f"invoices/{doc['id']}")["client_id"] == ""


def test_une_lecture_du_client_qui_echoue_refuse(fake, monkeypatch):
    """Strict : une panne de lecture ne passe jamais pour « le client
    existe » (ni pour « il n'existe pas »)."""
    def _boom(_cid):
        raise RuntimeError("Firestore indisponible")

    monkeypatch.setattr(invoice_model, "_read_client_strict", _boom)
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice("d1", [eid], [], _data())
    assert doc is None
    assert "lecture impossible" in errors[0]
    _nothing_written(fake, "timeentries/te1")


def test_la_lecture_du_client_ne_passe_pas_par_le_lecteur_permissif(fake):
    """get_partie avale une erreur en None : le plan lit par son propre
    client, strictement. Le faux enregistre la lecture de la fiche."""
    eid = _entry(fake, "te1")
    fake.reset_logs()
    invoice_model.plan_invoice(
        "d1", [eid], [], invoice_model.invoice_document_from(_data()),
        generated=True, require_all_sources=True)
    assert any("parties/p1" in r.paths for r in fake.reads)


def test_une_adresse_non_etablie_est_refusee(fake):
    """Le client existe, mais l'instantané de l'appelant est vide — sa propre
    lecture (permissive) de la fiche a échoué : jamais une adresse figée
    vide sur un document que le client détiendra."""
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(billing_address={"name": ""}))
    assert doc is None
    assert any("adresse de facturation du client n'a pas pu" in e
               for e in errors), errors


def test_un_nom_de_client_vide_est_refuse(fake):
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(client_name=""))
    assert doc is None
    assert any("nom du client" in e for e in errors), errors


# ══════════════════════════════════════════════════════════════════════
# 3. Les numéros d'inscription TPS/TVQ
# ══════════════════════════════════════════════════════════════════════


def test_des_numeros_de_taxe_vides_sont_refuses_et_nomment_parametres(fake):
    """(Régression : plus de cinquante factures émises sous des numéros
    vides.) Les deux refus ensemble : le juriste corrige tout d'un coup."""
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(gst_number="", qst_number="  "))
    assert doc is None
    joined = " ".join(errors)
    assert "numéro d'inscription TPS" in joined
    assert "numéro d'inscription TVQ" in joined
    assert "« Paramètres → Profil du cabinet »" in joined
    _nothing_written(fake, "timeentries/te1")


def test_une_facture_sans_taxe_n_exige_aucun_numero(fake):
    xid = _expense(fake, "ex1", taxable=False)
    doc, errors = invoice_model.create_invoice(
        "d1", [], [xid], _data(gst_number="", qst_number=""))
    assert errors == [], errors
    assert doc["gst_amount"] == doc["qst_amount"] == 0


def test_l_import_reproduit_des_numeros_vides(fake):
    """Le papier d'origine porte ce qu'il porte : l'import ne réémet rien."""
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(gst_number="", qst_number=""),
        invoice_number="2019-F007")
    assert errors == [], errors


# ══════════════════════════════════════════════════════════════════════
# 4. La provision est forcée à 0 sur le chemin généré
# ══════════════════════════════════════════════════════════════════════


def test_la_provision_est_forcee_a_zero_sur_le_chemin_genere(fake):
    """(Régression : l'ancien create_invoice stockait la provision et
    réduisait amount_due — puis le paiement d'honoraires du fidéicommis,
    vérifié contre le solde VIVANT, comptait le même argent une seconde
    fois.)"""
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(retainer_applied=10000))
    assert errors == [], errors
    stored = fake.peek(f"invoices/{doc['id']}")
    assert stored["retainer_applied"] == 0
    assert stored["amount_due"] == stored["total"]


def test_le_plan_dit_que_la_provision_a_ete_ignoree(fake):
    eid = _entry(fake, "te1")
    plan = invoice_model.plan_invoice(
        "d1", [eid], [],
        invoice_model.invoice_document_from(_data(retainer_applied=10000)),
        generated=True, require_all_sources=True)
    assert plan.errors == [] and plan.retainer_applied == 0
    assert any("provision" in w for w in plan.warnings), plan.warnings


def test_l_import_garde_sa_provision(fake):
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(retainer_applied=10000),
        invoice_number="2019-F007")
    assert errors == [], errors
    stored = fake.peek(f"invoices/{doc['id']}")
    assert stored["retainer_applied"] == 10000
    assert stored["amount_due"] == stored["total"] - 10000


# ══════════════════════════════════════════════════════════════════════
# 5. L'échéance, et le millésime du numéro
# ══════════════════════════════════════════════════════════════════════


def test_une_echeance_anterieure_a_la_date_est_refusee(fake):
    eid = _entry(fake, "te1")
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(due_date=datetime(2026, 6, 1, tzinfo=UTC)))
    assert doc is None
    assert any("2026-06-01" in e and "précède" in e for e in errors), errors


def test_le_numero_suit_l_annee_d_emission_et_le_dit(fake):
    """Une facture datée du 31 décembre, émise le 15 juin suivant, prend le
    numéro de l'année d'émission — c'est la doctrine (jamais la date), et
    c'est surprenant : l'avertissement le dit AVANT l'envoi."""
    eid = _entry(fake, "te1")
    dated = datetime(2025, 12, 31, tzinfo=UTC)
    plan = invoice_model.plan_invoice(
        "d1", [eid], [], invoice_model.invoice_document_from(_data(date=dated)),
        generated=True, require_all_sources=True)
    assert plan.errors == []
    assert any("datée de 2025" in w and "2026-F" in w for w in plan.warnings)
    doc, errors = invoice_model.create_invoice(
        "d1", [eid], [], _data(date=dated))
    assert errors == [] and doc["invoice_number"] == "2026-F001"
    assert "datée de 2025" in invoice_model.number_year_warning(
        doc["date"], doc["invoice_number"])


@pytest.mark.parametrize("when, number, flagged", [
    (datetime(2026, 1, 3, tzinfo=UTC), "2026-F004", False),
    (datetime(2025, 12, 31, tzinfo=UTC), "2026-F004", True),
    (date(2025, 12, 31), "2026-F004", True),
    (datetime(2019, 3, 1, tzinfo=UTC), "2019-F014", False),
    # Another system's numbering is not ours to comment on.
    (datetime(2019, 3, 1, tzinfo=UTC), "25160101", False),
    (datetime(2019, 3, 1, tzinfo=UTC), "2026-001-03", False),
    (None, "2026-F004", False),
    (datetime(2025, 12, 31, tzinfo=UTC), "", False),
])
def test_number_year_warning(when, number, flagged):
    assert bool(invoice_model.number_year_warning(when, number)) is flagged


# ══════════════════════════════════════════════════════════════════════
# 6. Une seule implémentation : le plan ne fait qu'y conduire
# ══════════════════════════════════════════════════════════════════════


def test_le_plan_n_ecrit_rien_et_n_alloue_rien(fake):
    eid = _entry(fake, "te1")
    fake.reset_logs()
    plan = invoice_model.plan_invoice(
        "d1", [eid], [], invoice_model.invoice_document_from(_data()),
        generated=True, require_all_sources=True)
    assert plan.errors == [] and plan.totals["total"] > 0
    assert fake.commits == []
    assert not any(COUNTER in r.paths for r in fake.reads)


def test_un_plan_refuse_rapporte_ce_qu_il_a_pu_calculer(fake):
    """L'aperçu (lot 3b) montre POURQUOI : le plan refusé garde la raison de
    chaque source écartée."""
    ok = _entry(fake, "te-ok")
    nf = _entry(fake, "te-nf", billable=False)
    plan = invoice_model.plan_invoice(
        "d1", [ok, nf], [], invoice_model.invoice_document_from(_data()),
        generated=True, require_all_sources=True)
    assert plan.errors and plan.refusal_reason == "sources_inutilisables"
    assert plan.skipped == {"te-nf": "non facturable"}
    assert plan.valid_entry_ids == ["te-ok"]


def test_create_invoice_decide_par_plan_invoice_et_par_lui_seul():
    """Une seule implémentation : create_invoice appelle plan_invoice et ne
    lit plus aucune source lui-même — sinon l'aperçu et l'écriture
    pourraient diverger (la leçon du dry_run retiré)."""
    tree = ast.parse((_ATHENA / "models" / "invoice.py").read_text(
        encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "create_invoice")
    called = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr
              for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, (ast.Name, ast.Attribute))}
    assert {"plan_invoice", "invoice_document_from"} <= called
    assert not called & {"get_time_entry", "get_expense", "issuance_refusals",
                         "compute_totals", "_adjustment_line_item",
                         "_sanitize_data", "_default_doc"}


def test_l_apercu_et_l_ecriture_jugent_le_meme_document(fake):
    """(Revue du lot 3a : plan_invoice laissait chaque appelant bâtir son
    propre document — l'aperçu du lot 3b en aurait assemblé un sans la liste
    blanche ni le nettoyage de create_invoice, et jugé une AUTRE facture.)
    Le document jugé est celui d'invoice_document_from : ici un nom de
    client fait de balises, que le nettoyage vide — refusé par les deux, au
    même motif, et un statut fourni qui ne franchit pas la liste blanche."""
    eid = _entry(fake, "te1")
    data = _data(client_name="<b></b>", status="payée")
    doc = invoice_model.invoice_document_from(data)
    assert doc["client_name"] == "" and doc["status"] == "brouillon"
    plan = invoice_model.plan_invoice(
        "d1", [eid], [], doc, generated=True, require_all_sources=True)
    _, errors = invoice_model.create_invoice(
        "d1", [eid], [], data, require_all_sources=True)
    assert plan.refusal_reason == "nom_client_vide"
    assert plan.errors == errors
    _nothing_written(fake, f"timeentries/{eid}")


def test_les_refus_d_emission_sont_purs():
    """issuance_refusals ne lit rien : ce qu'il juge lui est donné."""
    merged = invoice_model.invoice_document_from(_data(gst_number=""))
    out = invoice_model.issuance_refusals(
        merged, {"gst_amount": 1500, "qst_amount": 0},
        generated=True, client={"id": "p1"})
    assert [r for _, r in out] == ["numero_tps_vide"]
    assert invoice_model.issuance_refusals(
        merged, {"gst_amount": 1500}, generated=False, client={"id": "p1"}
    ) == []


# ══════════════════════════════════════════════════════════════════════
# 7. Le journal : identifiants, comptes et codes — jamais un montant
# ══════════════════════════════════════════════════════════════════════


def _invoice_events(caplog) -> list[dict]:
    return [r.json_fields for r in caplog.records
            if r.name == "pallas.invoice"]


def test_la_creation_et_ses_refus_laissent_une_trace_sans_montant(fake, caplog):
    caplog.set_level("INFO", logger="pallas.invoice")
    eid = _entry(fake, "te1")
    invoice_model.create_invoice("d1", [eid], [], _data(gst_number=""))
    refused = _invoice_events(caplog)[-1]
    assert refused["event"] == "invoice_refused"
    assert (refused["operation"], refused["reason"]) == ("create", "numero_tps_vide")
    assert refused["dossier_id"] == "d1"

    caplog.clear()
    doc, _ = invoice_model.create_invoice("d1", [eid], [], _data())
    created = _invoice_events(caplog)[-1]
    assert created["event"] == "invoice_created"
    assert created["invoice_id"] == doc["id"]
    assert (created["source_count"], created["generated_number"]) == (1, True)
    blob = str(created)
    assert "2026-F001" not in blob and str(doc["total"]) not in blob
    assert "Tremblay" not in blob


# ══════════════════════════════════════════════════════════════════════
# 8. Le formulaire web
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(fake):
    fake.seed("settings/cabinet", {"nom": "Me Test", "gst_number": GST,
                                   "qst_number": QST})
    fake.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "status": "actif", "clients": [{"id": "p1", "name": "Jean Tremblay"}],
        "client_ids": ["p1"], "opposing_parties": [],
        "opposing_party_ids": [],
    })
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    for bp in (invoices_routes.invoices_bp, admin_ledger_routes.admin_bp,
               dossiers_routes.dossiers_bp, parties_routes.parties_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _post(client, *entries, **form):
    data = {"dossier_id": "d1", "invoice_date": "2026-06-15",
            "selected_entries": list(entries), **form}
    return client.post("/factures/", data=data)


def test_le_formulaire_emet_une_facture_a_son_client(fake, client):
    eid = _entry(fake, "te1")
    resp = _post(client, eid)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    (stored,) = fake.peek_collection("invoices").values()
    assert stored["client_id"] == "p1"
    assert stored["billing_address"]["street"] == "1 rue X"
    assert (stored["gst_number"], stored["qst_number"]) == (GST, QST)
    assert stored["retainer_applied"] == 0


def test_le_formulaire_refuse_une_selection_perimee_par_son_nom(fake, client):
    """(Régression : sans require_all_sources, l'entrée facturée dans un
    autre onglet était écartée EN SILENCE et la facture émise plus courte
    que la page.)"""
    ok = _entry(fake, "te-ok")
    stale = _entry(fake, "te-stale", invoiced=True, invoice_id="inv-autre")
    resp = _post(client, ok, stale)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Sources inutilisables" in html
    assert "facture inv-autre" in html
    assert fake.peek_collection("invoices") == {}
    assert fake.peek(f"timeentries/{ok}")["invoiced"] is False


def test_un_refus_rend_la_selection_et_les_saisies_de_l_avocat(fake, client):
    """(Régression, revue du lot 3a : la page coche d'office TOUT le non
    facturé, et un refus la re-rendait ainsi. L'entrée que l'avocat avait
    délibérément laissée de côté était donc RECOCHÉE en silence après un
    refus — une sélection périmée, un numéro de taxe vide… — et le « Créer »
    suivant la facturait. Ses notes, ses conditions et son échéance
    disparaissaient aussi.)"""
    ok = _entry(fake, "te-ok")
    _entry(fake, "te-laissee-de-cote")                  # listed, NOT chosen
    stale = _entry(fake, "te-stale", invoiced=True, invoice_id="inv-autre")
    x_ok = _expense(fake, "x-ok")
    _expense(fake, "x-laisse")
    resp = client.post("/factures/", data={
        "dossier_id": "d1", "invoice_date": "2026-06-15",
        "selected_entries": [ok, stale], "selected_expenses": [x_ok],
        "notes": "Mes notes pour le client",
        "payment_terms": "Payable à réception.",
        "due_date": "2026-07-01",
    })
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Sources inutilisables" in html
    # The lawyer's own selection — the stale id is gone with its checkbox.
    assert 'selectedEntries: ["te-ok"]' in html
    assert 'selectedExpenses: ["x-ok"]' in html
    assert "Mes notes pour le client" in html
    assert "Payable à réception." in html
    assert 'name="due_date" value="2026-07-01"' in html


def test_le_premier_affichage_coche_tout_le_non_facture(fake, client):
    """La valeur par défaut, inchangée : rien n'a été soumis, tout est
    coché."""
    _entry(fake, "te-a")
    _entry(fake, "te-b")
    html = client.get("/factures/new?dossier_id=d1").get_data(as_text=True)
    (initial,) = re.findall(r"selectedEntries: (\[[^\]]*\])", html)
    assert sorted(json.loads(initial)) == ["te-a", "te-b"]
    assert "Payable dans les 30 jours suivant la date de facturation." in html


def test_le_formulaire_n_offre_ni_n_envoie_de_provision(fake, client):
    eid = _entry(fake, "te1")
    page = client.get("/factures/new?dossier_id=d1").get_data(as_text=True)
    assert 'name="retainer_applied"' not in page
    _post(client, eid, retainer_applied="100.00")      # a forged field
    (stored,) = fake.peek_collection("invoices").values()
    assert stored["retainer_applied"] == 0


def test_le_formulaire_refuse_sans_numeros_de_taxe(fake, client):
    fake.seed("settings/cabinet", {"nom": "Me Test", "gst_number": "",
                                   "qst_number": ""})
    eid = _entry(fake, "te1")
    resp = _post(client, eid)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Paramètres → Profil du cabinet" in html
    assert fake.peek_collection("invoices") == {}


def test_une_facture_datee_d_une_autre_annee_arrive_sur_sa_fiche_avec_l_avertissement(
    fake, client,
):
    eid = _entry(fake, "te1")
    resp = _post(client, eid, invoice_date="2025-12-31",
                 return_to="/dossiers/d1")
    assert resp.status_code == 302
    (stored,) = fake.peek_collection("invoices").values()
    loc = urlparse(resp.location)
    assert loc.path == f"/factures/{stored['id']}"
    assert "datée de 2025" in parse_qs(loc.query)["message"][0]


def test_sans_avertissement_le_retour_demande_est_honore(fake, client):
    eid = _entry(fake, "te1")
    resp = _post(client, eid, return_to="/dossiers/d1")
    assert resp.status_code == 302
    assert urlparse(resp.location).path == "/dossiers/d1"
