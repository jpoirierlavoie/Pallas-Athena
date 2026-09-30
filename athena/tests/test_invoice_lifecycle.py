"""Le cycle de vie d'une facture : statut, annulation, création (lot 0b).

Quatre défauts silencieux du modèle, chacun atteignable depuis le web
aujourd'hui et que le connecteur rendrait routiniers :

1. ``update_status(id, "annulée")`` basculait le statut SANS libérer les
   sources : ``void_invoice`` refusait ensuite « déjà annulée » et
   ``delete_invoice`` refusait les références restées accrochées — les
   heures restaient facturées pour toujours. La route ``/status`` l'acceptait.
2. ``void_invoice`` ne regardait que le statut, jamais l'argent : annuler une
   facture partiellement payée libérait ses heures pour une nouvelle
   facturation pendant que le paiement restait sur la facture annulée.
3. ``update_status`` lisait puis écrivait hors transaction : une bascule
   automatique à « payée » survenue entre les deux était écrasée.
4. ``create_invoice`` stockait ``status``/``amount_paid``/``paid_date``
   tels que l'appelant les donnait.

Tout passe par le faux Firestore partagé (``tests/_fake_firestore.py`` — le
client, ses transactions et la boucle de reprise de ``transactional`` sont
les vrais) ; une « écriture rivale » est posée directement dans le magasin
par un crochet de commit, et l'on relit ce qui est STOCKÉ.
"""

import os
import pathlib
import re
import sys
from datetime import datetime, timezone
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
    from models import admin_ledger as admin_ledger_model
    from models import concurrency
    from models import expense as expense_model
    from models import invoice as invoice_model
    from models import time_entry as time_entry_model
    from models import trust as trust_model
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.parties as parties_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it.
_LOADED_UNDER_FAKE = (expense_model, time_entry_model)

UTC = timezone.utc
WHEN = datetime(2026, 6, 15, tzinfo=UTC)
INV = "inv1"
ETAG = "aaaaaaaa-1111-4222-8333-444444444444"
RIVAL = "bbbbbbbb-1111-4222-8333-444444444444"


# ══════════════════════════════════════════════════════════════════════
# Le banc
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model a
    route starts to read tomorrow cannot reach the mocked client instead."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _invoice(fake, **over) -> dict:
    doc = {
        "id": INV, "invoice_number": "2026-F031", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "T c. L",
        "client_id": "p1", "client_name": "Jean",
        "billing_address": {"name": "Jean", "street": "", "unit": "",
                            "city": "", "province": "QC", "postal_code": ""},
        "date": WHEN, "due_date": WHEN, "status": "envoyée",
        "subtotal_fees": 30000, "subtotal_expenses": 5000, "subtotal": 35000,
        "gst_rate": 500, "gst_amount": 1750, "qst_rate": 9975,
        "qst_amount": 3491, "total": 40241, "retainer_applied": 0,
        "amount_due": 40241, "amount_paid": 0, "paid_date": None,
        "gst_number": "", "qst_number": "", "notes": "", "payment_terms": "",
        "etag": ETAG, "created_at": WHEN, "updated_at": WHEN,
    }
    doc.update(over)
    fake.seed(f"invoices/{INV}", doc)
    return doc


def _source(fake, collection: str, sid: str, *, invoice_id=INV, **over):
    doc = {"id": sid, "dossier_id": "d1", "date": WHEN, "description": "R",
           "amount": 30000, "invoiced": True, "invoice_id": invoice_id,
           "etag": f"etag-{sid}"}
    doc.update(over)
    fake.seed(f"{collection}/{sid}", doc)


def _line(fake, lid: str, source_id: str, kind: str = "fee"):
    """A line item shaped as create_invoice writes it."""
    fake.seed(f"invoices/{INV}/lineitems/{lid}", {
        "id": lid, "type": kind, "source_id": source_id, "date": WHEN,
        "description": "R", "amount": 30000, "taxable": True,
        "hours": 1.0 if kind == "fee" else None,
        "rate": 30000 if kind == "fee" else None,
    })


def _billed_invoice(fake, **over) -> dict:
    """An envoyée invoice with one fee and one disbursement line."""
    doc = _invoice(fake, **over)
    _source(fake, "timeentries", "te1")
    _source(fake, "expenses", "ex1", amount=5000)
    _line(fake, "li1", "te1", "fee")
    _line(fake, "li2", "ex1", "expense")
    return doc


def _unchanged(fake, before: dict) -> None:
    assert fake.peek(f"invoices/{INV}") == before
    assert fake.peek("timeentries/te1")["invoiced"] is True
    assert fake.peek("timeentries/te1")["invoice_id"] == INV
    assert fake.peek("expenses/ex1")["invoiced"] is True


# ══════════════════════════════════════════════════════════════════════
# 1. update_status refuse « annulée » — d'entrée
# ══════════════════════════════════════════════════════════════════════


def test_update_status_refuse_annulee_avant_toute_lecture(fake):
    """(Régression vérifiée en rétablissant l'ancien update_status : il
    écrivait « annulée » et laissait te1/ex1 facturées pour toujours.)"""
    _billed_invoice(fake)
    before = fake.peek(f"invoices/{INV}")
    fake.reset_logs()
    ok, err = invoice_model.update_status(INV, "annulée")
    assert ok is False
    assert err == invoice_model.VOID_BY_STATUS_REFUSED
    assert "« Annuler »" in err
    assert fake.reads == [] and fake.commits == []
    _unchanged(fake, before)


def test_la_route_statut_refuse_annulee_et_le_dit(fake, client):
    _billed_invoice(fake)
    before = fake.peek(f"invoices/{INV}")
    resp = client.post(f"/factures/{INV}/status", data={"status": "annulée"})
    assert resp.status_code == 302
    erreur = parse_qs(urlparse(resp.location).query)["erreur"][0]
    assert erreur == invoice_model.VOID_BY_STATUS_REFUSED
    _unchanged(fake, before)
    page = client.get(resp.location).get_data(as_text=True)
    assert "Une facture ne s&#39;annule pas par un changement de statut" in page


def test_la_route_statut_ne_perd_plus_un_refus_en_silence(fake, client):
    """Avant : la branche non-htmx redirigeait SANS rien dire, et la branche
    htmx rendait un 422 que htmx n'échange jamais."""
    _invoice(fake, status="brouillon")
    resp = client.post(f"/factures/{INV}/status", data={"status": "en_retard"})
    erreur = parse_qs(urlparse(resp.location).query)["erreur"][0]
    assert "non permise" in erreur
    htmx = client.post(f"/factures/{INV}/status", data={"status": "en_retard"},
                       headers={"HX-Request": "true"})
    assert htmx.status_code == 302
    assert "erreur=" in htmx.headers["HX-Redirect"]


# ══════════════════════════════════════════════════════════════════════
# 2. update_status est transactionnel, avec son etag attendu
# ══════════════════════════════════════════════════════════════════════


def test_update_status_compare_l_etag_dans_la_transaction(fake):
    _invoice(fake)
    before = fake.peek(f"invoices/{INV}")
    ok, err = invoice_model.update_status(INV, "en_retard", expected_etag=RIVAL)
    assert ok is False and err == concurrency.STALE_ETAG_ERROR
    assert fake.peek(f"invoices/{INV}") == before

    ok, err = invoice_model.update_status(INV, "en_retard", expected_etag=ETAG)
    assert (ok, err) == (True, "")
    stored = fake.peek(f"invoices/{INV}")
    assert stored["status"] == "en_retard" and stored["etag"] != ETAG
    assert stored["updated_via"] == "script"


def test_update_status_sans_etag_reste_le_chemin_historique(fake):
    """Un script (reprise_encaissements) et une page rendue avant le
    déploiement n'en portent pas : aucune comparaison, comme avant."""
    _invoice(fake, status="payée", amount_paid=0)
    assert invoice_model.update_status(INV, "envoyée") == (True, "")


def test_une_bascule_a_payee_concurrente_n_est_plus_ecrasee(fake):
    """(Régression vérifiée en rétablissant l'ancien update_status : son
    update() hors transaction écrasait « payée » par « en retard ».)"""
    _invoice(fake)
    fired = []

    def _record_payment_lands(info):
        if not fired and any(p == f"invoices/{INV}" for _k, p in info.ops):
            fired.append(info.index)
            doc = fake.peek(f"invoices/{INV}")
            doc.update(status="payée", amount_paid=40241, etag=RIVAL)
            fake.external_write(f"invoices/{INV}", doc)

    remove = fake.add_commit_hook(_record_payment_lands)
    try:
        ok, err = invoice_model.update_status(INV, "en_retard")
    finally:
        remove()
    assert fired
    assert ok is False
    assert "Payée" in err and "non permise" in err
    assert fake.peek(f"invoices/{INV}")["status"] == "payée"


def test_la_route_statut_transmet_l_etag_du_formulaire(fake, client):
    _invoice(fake)
    before = fake.peek(f"invoices/{INV}")
    resp = client.post(f"/factures/{INV}/status",
                       data={"status": "en_retard", "expected_etag": RIVAL})
    erreur = parse_qs(urlparse(resp.location).query)["erreur"][0]
    assert erreur == concurrency.STALE_ETAG_ERROR
    assert fake.peek(f"invoices/{INV}") == before
    ok = client.post(f"/factures/{INV}/status",
                     data={"status": "en_retard", "expected_etag": ETAG})
    assert "erreur" not in ok.location
    assert fake.peek(f"invoices/{INV}")["status"] == "en_retard"


def test_un_etag_malforme_est_un_400_et_rien_n_est_ecrit(fake, client):
    _invoice(fake)
    before = fake.peek(f"invoices/{INV}")
    resp = client.post(f"/factures/{INV}/status",
                       data={"status": "en_retard", "expected_etag": "<x>"})
    assert resp.status_code == 400
    assert fake.peek(f"invoices/{INV}") == before


# ══════════════════════════════════════════════════════════════════════
# 3. available_transitions n'offre plus une annulation que void refuserait
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status,expected", [
    ("envoyée", ("en_retard",)),
    ("en_retard", ("envoyée",)),
    ("payée", ()),
])
def test_un_paiement_inscrit_retire_annuler(status, expected):
    inv = {"status": status, "amount_paid": 100}
    assert invoice_model.available_transitions(inv) == expected


def test_une_ecriture_debout_retire_annuler_meme_a_zero():
    """La dérive : une écriture tient encore alors qu'amount_paid vaut 0.
    La fiche remet les écritures qu'elle lit déjà."""
    inv = {"status": "envoyée", "amount_paid": 0}
    debout = {"kind": "encaissement_facture", "status": "compensée"}
    contre_passee = dict(debout, reversed_by_id="r1")
    assert invoice_model.available_transitions(inv, receipts=[debout]) == (
        "en_retard",)
    assert invoice_model.available_transitions(
        inv, receipts=[contre_passee]) == ("en_retard", "annulée")
    assert invoice_model.available_transitions(inv) == ("en_retard", "annulée")


def test_la_fiche_n_affiche_pas_annuler_sur_une_facture_payee_en_partie(
    fake, client,
):
    _billed_invoice(fake, amount_paid=10000)
    page = client.get(f"/factures/{INV}").get_data(as_text=True)
    assert f"/factures/{INV}/void" not in page
    assert f"/factures/{INV}/status" in page       # « Marquer en retard »


def test_un_paiement_d_honoraires_debout_retire_annuler():
    """Le cas que ni amount_paid ni les écritures d'administration ne voient :
    la recette automatique d'un paiement d'honoraires a échoué (fail-open)."""
    inv = {"status": "envoyée", "amount_paid": 0}
    debout = {"purpose": "virement_honoraires", "status": "compensée"}
    assert invoice_model.available_transitions(inv, trust_payments=[debout]) == (
        "en_retard",)
    for inerte in (dict(debout, reversed_by_id="r1"),
                   dict(debout, status="annulée"),
                   {"purpose": "correction", "status": "en_circulation"}):
        assert invoice_model.available_transitions(
            inv, trust_payments=[inerte]) == ("en_retard", "annulée")


def test_la_fiche_n_affiche_pas_annuler_sous_un_paiement_d_honoraires_seul(
    fake, client,
):
    """Revue B1 : l'étape livrée laissait « Annuler » s'afficher pour être
    refusé dans ce cas — la fiche ne lisait que les écritures
    d'administration. (Vérifié en retirant la lecture du fidéicommis de la
    route : le formulaire /void revenait et le test tombait.)"""
    _billed_invoice(fake)
    fake.seed("trust_transactions/t1", {
        "invoice_id": INV, "purpose": "virement_honoraires",
        "status": "compensée", "amount": 10000,
    })
    page = client.get(f"/factures/{INV}").get_data(as_text=True)
    assert f"/factures/{INV}/void" not in page
    assert f"/factures/{INV}/status" in page       # « Marquer en retard »


def test_la_fiche_ne_lit_le_fidéicommis_que_si_annuler_serait_offert(
    fake, client,
):
    """Une requête de plus seulement quand elle peut changer la réponse.
    (Une requête n'inscrit que les documents qu'elle RENVOIE : la ligne semée
    est ce qui rend son absence probante.)"""
    _billed_invoice(fake, amount_paid=10000)
    fake.seed("trust_transactions/t1", {
        "invoice_id": INV, "purpose": "virement_honoraires",
        "status": "compensée", "amount": 10000,
    })
    fake.reset_logs()
    client.get(f"/factures/{INV}")
    assert not any("trust_transactions" in p
                   for r in fake.reads for p in r.paths), [
        r.paths for r in fake.reads]


def test_les_formulaires_de_la_fiche_portent_l_etag(fake, client):
    _billed_invoice(fake)
    page = client.get(f"/factures/{INV}").get_data(as_text=True)
    etags = re.findall(r'name="expected_etag" value="([^"]*)"', page)
    # « Marquer en retard » + « Annuler ».
    assert etags == [ETAG, ETAG]


# ══════════════════════════════════════════════════════════════════════
# 4. void_invoice regarde l'argent — et ne refuse jamais à cause d'une source
# ══════════════════════════════════════════════════════════════════════


def test_une_facture_payee_en_partie_ne_s_annule_pas(fake):
    """(Régression vérifiée en rétablissant l'ancien void_invoice : il
    annulait et libérait te1/ex1 pendant que les 100,00 $ restaient sur la
    facture annulée — le client refacturé pour le même travail.)"""
    _billed_invoice(fake, amount_paid=10000)
    fake.seed("admin_transactions/a1", {
        "invoice_id": INV, "kind": "encaissement_facture",
        "status": "compensée", "amount": 10000,
    })
    before = fake.peek(f"invoices/{INV}")
    report, errors = invoice_model.void_invoice_report(INV)
    assert report is None
    assert "« Administration »" in errors[0]
    assert format_cents_fr(10000) in errors[0]
    _unchanged(fake, before)


def test_un_paiement_d_honoraires_du_fideicommis_renvoie_au_fideicommis(fake):
    _billed_invoice(fake, amount_paid=10000)
    fake.seed("trust_transactions/t1", {
        "invoice_id": INV, "purpose": "virement_honoraires",
        "status": "compensée", "amount": 10000,
    })
    fake.seed("admin_transactions/a1", {
        "invoice_id": INV, "kind": "encaissement_facture",
        "status": "compensée", "amount": 10000, "trust_transaction_id": "t1",
    })
    before = fake.peek(f"invoices/{INV}")
    report, errors = invoice_model.void_invoice_report(INV)
    assert report is None
    assert "« Fidéicommis »" in errors[0]
    assert "Administration" not in errors[0].split("(")[0]
    _unchanged(fake, before)


def test_un_paiement_d_honoraires_sans_recette_bloque_et_renvoie_au_fideicommis(
    fake,
):
    """Le fidéicommis SEUL : amount_paid à 0, aucune écriture d'administration
    (la recette automatique a échoué). Le test voisin sème les trois signaux à
    la fois, si bien qu'aucun ne s'y prouvait isolément : retirer la lecture
    du fidéicommis le laissait vert. (Vérifié : sans elle, celui-ci annule et
    libère te1/ex1.)"""
    _billed_invoice(fake)
    fake.seed("trust_transactions/t1", {
        "invoice_id": INV, "purpose": "virement_honoraires",
        "status": "en_circulation", "amount": 10000,
    })
    before = fake.peek(f"invoices/{INV}")
    report, errors = invoice_model.void_invoice_report(INV)
    assert report is None
    assert "« Fidéicommis »" in errors[0]
    _unchanged(fake, before)


def test_une_ecriture_d_administration_debout_bloque_meme_a_zero(fake):
    """La dérive : amount_paid à 0 mais une écriture tient encore."""
    _billed_invoice(fake)
    fake.seed("admin_transactions/a1", {
        "invoice_id": INV, "kind": "encaissement_facture",
        "status": "en_circulation", "amount": 10000,
    })
    report, errors = invoice_model.void_invoice_report(INV)
    assert report is None
    assert "« Administration »" in errors[0]


def test_un_historique_contre_passe_n_empeche_pas_l_annulation(fake):
    _billed_invoice(fake)
    fake.seed("admin_transactions/a1", {
        "invoice_id": INV, "kind": "encaissement_facture",
        "status": "compensée", "amount": 10000, "reversed_by_id": "a2",
    })
    fake.seed("trust_transactions/t1", {
        "invoice_id": INV, "purpose": "virement_honoraires",
        "status": "annulée", "amount": 10000,
    })
    report, errors = invoice_model.void_invoice_report(INV)
    assert errors == [], errors
    assert fake.peek(f"invoices/{INV}")["status"] == "annulée"


def test_une_source_portee_a_une_autre_facture_est_laissee_et_signalee(fake):
    """LE piège que la revue a attrapé : refuser l'annulation à cause d'une
    source « incohérente » rendait la facture inannulable pour toujours —
    précisément la victime de la course du formulaire de temps, et le
    doublon qu'elle a produit. L'annulation PASSE ; la source étrangère
    n'est pas touchée et le rapport la nomme."""
    _invoice(fake)
    _source(fake, "timeentries", "te1")                           # la sienne
    _source(fake, "timeentries", "te2", invoice_id="inv-other")   # refacturée
    _source(fake, "timeentries", "te3", invoice_id=None, invoiced=False)
    _source(fake, "expenses", "ex9")        # la nomme, mais aucune ligne
    for lid, sid, kind in (("l1", "te1", "fee"), ("l2", "te2", "fee"),
                           ("l3", "te3", "fee"), ("l4", "gone", "expense")):
        _line(fake, lid, sid, kind)
    fake.seed(f"invoices/{INV}/lineitems/adj", {
        "id": "adj", "type": "fee", "source_id": "", "amount": -500,
    })
    other = fake.peek("timeentries/te2")

    report, errors = invoice_model.void_invoice_report(INV)
    assert errors == [], errors
    assert report["released_time_entry_ids"] == ["te1", "te3"]
    assert report["released_expense_ids"] == ["ex9"]
    assert report["foreign_source_ids"] == ["te2"]
    assert report["missing_source_ids"] == ["gone"]
    assert fake.peek("timeentries/te2") == other, "une source étrangère touchée"
    for path in ("timeentries/te1", "timeentries/te3", "expenses/ex9"):
        stored = fake.peek(path)
        assert stored["invoiced"] is False and stored["invoice_id"] is None
    assert fake.peek(f"invoices/{INV}")["status"] == "annulée"
    assert fake.peek("expenses/gone") is None      # rien de créé


def test_une_source_deja_liberee_n_est_pas_reecrite(fake):
    """Revue B1 : la victime de la course (invoiced False, sans invoice_id)
    était « libérée » par une écriture — etag régénéré, updated_via tamponné
    sur une ligne que rien ne changeait, et un formulaire d'édition ouvert
    sur elle refusait ensuite sa sauvegarde comme périmée."""
    _invoice(fake)
    _source(fake, "timeentries", "te1")
    _source(fake, "timeentries", "te3", invoice_id=None, invoiced=False)
    _line(fake, "l1", "te1")
    _line(fake, "l3", "te3")
    untouched = fake.peek("timeentries/te3")
    report, errors = invoice_model.void_invoice_report(INV)
    assert errors == [], errors
    assert report["released_time_entry_ids"] == ["te1", "te3"]
    assert fake.peek("timeentries/te3") == untouched
    assert fake.peek("timeentries/te1")["etag"] != "etag-te1"
    written = {path for c in fake.commits for _kind, path in c.ops}
    assert "timeentries/te3" not in written


def test_apres_l_annulation_la_suppression_ne_trouve_aucune_source_accrochee(
    fake,
):
    _billed_invoice(fake)
    _source(fake, "timeentries", "stranded")   # la nomme, sans ligne
    assert invoice_model.void_invoice(INV) == (True, "")
    assert invoice_model.delete_invoice(INV) == (True, "")


def test_des_lignes_introuvables_sous_un_sous_total_non_nul_refusent(fake):
    _invoice(fake)
    _source(fake, "timeentries", "te1")
    before = fake.peek(f"invoices/{INV}")
    report, errors = invoice_model.void_invoice_report(INV)
    assert report is None and "introuvables" in errors[0]
    assert fake.peek(f"invoices/{INV}") == before


def test_void_compare_l_etag_attendu(fake):
    _billed_invoice(fake)
    before = fake.peek(f"invoices/{INV}")
    assert invoice_model.void_invoice(INV, expected_etag=RIVAL) == (
        False, concurrency.STALE_ETAG_ERROR)
    _unchanged(fake, before)
    assert invoice_model.void_invoice(INV, expected_etag=ETAG) == (True, "")


def test_un_encaissement_concurrent_fait_echouer_l_annulation(fake):
    """(Régression vérifiée en rétablissant l'ancien void_invoice : son lot
    hors transaction annulait la facture et libérait ses heures alors que le
    paiement venait d'y être porté.)"""
    _billed_invoice(fake)
    fired = []

    def _payment_lands(info):
        if not fired and any(p == f"invoices/{INV}" for _k, p in info.ops):
            fired.append(info.index)
            doc = fake.peek(f"invoices/{INV}")
            doc.update(amount_paid=10000, etag=RIVAL)
            fake.external_write(f"invoices/{INV}", doc)

    remove = fake.add_commit_hook(_payment_lands)
    try:
        ok, err = invoice_model.void_invoice(INV)
    finally:
        remove()
    assert fired
    assert ok is False and "Administration" in err
    assert fake.peek(f"invoices/{INV}")["status"] == "envoyée"
    assert fake.peek("timeentries/te1")["invoiced"] is True


@pytest.mark.parametrize("status,needle", [
    ("annulée", "déjà annulée"),
    ("payée", "déjà payée"),
])
def test_void_refuse_une_facture_close(fake, status, needle):
    _billed_invoice(fake, status=status)
    ok, err = invoice_model.void_invoice(INV)
    assert ok is False and needle in err


def test_la_route_annuler_dit_son_refus(fake, client):
    _billed_invoice(fake, amount_paid=10000)
    resp = client.post(f"/factures/{INV}/void")
    erreur = parse_qs(urlparse(resp.location).query)["erreur"][0]
    assert "Administration" in erreur
    page = client.get(resp.location).get_data(as_text=True)
    assert "contre-passez l&#39;écriture" in page
    assert fake.peek(f"invoices/{INV}")["status"] == "envoyée"


def test_la_route_annuler_signale_ce_qu_elle_a_laisse_de_cote(fake, client):
    _invoice(fake)
    _source(fake, "timeentries", "te1")
    _source(fake, "timeentries", "te2", invoice_id="inv-other")
    _line(fake, "l1", "te1")
    _line(fake, "l2", "te2")
    resp = client.post(f"/factures/{INV}/void")
    message = parse_qs(urlparse(resp.location).query)["message"][0]
    assert message.startswith("Facture annulée.") and "te2" in message
    page = client.get(resp.location).get_data(as_text=True)
    assert "AUTRE facture" in page
    assert fake.peek(f"invoices/{INV}")["status"] == "annulée"


def test_la_route_supprimer_dit_son_refus(fake, client):
    """Revue B1 : /delete avait le même défaut que /status et /void — la
    branche non-htmx redirigeait sans rien, la branche htmx rendait un 422
    que htmx n'échange jamais. (Vérifié sur l'ancienne route : aucun
    `erreur=` dans la redirection.)"""
    _invoice(fake, status="annulée")
    _source(fake, "timeentries", "stranded")      # la nomme encore
    resp = client.post(f"/factures/{INV}/delete")
    assert resp.status_code == 302
    erreur = parse_qs(urlparse(resp.location).query)["erreur"][0]
    assert "référencent encore" in erreur
    page = client.get(resp.location).get_data(as_text=True)
    assert "référencent encore cette" in page
    assert fake.peek(f"invoices/{INV}") is not None
    htmx = client.post(f"/factures/{INV}/delete",
                       headers={"HX-Request": "true"})
    assert htmx.status_code == 302 and "erreur=" in htmx.headers["HX-Redirect"]


def test_une_annulation_sans_reste_ne_porte_aucun_bandeau(fake, client):
    _billed_invoice(fake)
    resp = client.post(f"/factures/{INV}/void")
    assert "message" not in resp.location and "erreur" not in resp.location


def test_les_bandeaux_de_la_fiche_n_emploient_que_des_classes_compilees(
    fake, client,
):
    """Une classe absente de l'artefact compilé ne s'applique pas, en
    silence — et en ajouter une est un éventail de sept fichiers (CLAUDE.md,
    point 6). Les deux bandeaux réutilisent des chaînes déjà compilées."""
    _billed_invoice(fake)
    page = client.get(
        f"/factures/{INV}?erreur=refus&message=reste"
    ).get_data(as_text=True)
    banners = re.findall(r'<div role="(?:alert|status)" class="([^"]+)"', page)
    assert len(banners) == 2, banners
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    absent = []
    for c in sorted({c for block in banners for c in block.split()}):
        needle = "." + c.replace(":", r"\:").replace("/", r"\/")
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert not absent, absent


def _invoice_events(caplog) -> list[dict]:
    return [r.json_fields for r in caplog.records
            if r.name == "pallas.invoice"]


def test_l_annulation_et_ses_refus_laissent_une_trace_sans_montant(fake, caplog):
    """Avant ce lot, un changement de statut et une annulation — les deux
    opérations qui décident si des heures redeviennent facturables — ne
    laissaient AUCUNE ligne de journal. Identifiants, comptes et codes
    seulement : jamais un montant, jamais un numéro de facture."""
    caplog.set_level("INFO", logger="pallas.invoice")
    _billed_invoice(fake, amount_paid=10000)
    invoice_model.void_invoice(INV)
    refused = _invoice_events(caplog)[-1]
    assert refused["event"] == "invoice_refused"
    assert refused["operation"] == "void"
    assert refused["reason"] == "paiement_inscrit"
    assert "10000" not in str(refused) and "2026-F031" not in str(refused)

    caplog.clear()
    doc = fake.peek(f"invoices/{INV}")
    doc["amount_paid"] = 0
    fake.external_write(f"invoices/{INV}", doc)
    _source(fake, "timeentries", "te2", invoice_id="inv-other")
    _line(fake, "li3", "te2")
    assert invoice_model.void_invoice(INV) == (True, "")
    voided = _invoice_events(caplog)[-1]
    assert voided["event"] == "invoice_voided"
    assert (voided["released_count"], voided["foreign_count"],
            voided["missing_count"]) == (2, 1, 0)

    caplog.clear()
    invoice_model.update_status(INV, "annulée")
    assert _invoice_events(caplog)[-1]["reason"] == "annulation_par_statut"


# ══════════════════════════════════════════════════════════════════════
# 5. create_invoice : l'état de paiement est FORCÉ, les clés sont triées
# ══════════════════════════════════════════════════════════════════════


def test_create_invoice_force_l_etat_de_paiement(fake):
    """(Régression vérifiée en rétablissant l'ancienne fusion : la facture
    naissait « payée », avec 402,41 $ inscrits sur aucun registre.)"""
    _source(fake, "timeentries", "te1", invoiced=False, invoice_id=None,
            hours=1.0, rate=30000, billable=True)
    # The client is on file: since lot 3a create_invoice reads it strictly,
    # on the import path too, and refuses one that does not resolve.
    fake.seed("parties/p1", {"id": "p1", "type": "individual",
                             "first_name": "Jean", "last_name": "T"})
    invoice, errors = invoice_model.create_invoice(
        "d1", ["te1"], [],
        {"dossier_id": "d1", "date": WHEN, "client_id": "p1",
         "client_name": "Jean", "legacy_ref": "OLD-1",
         # What no caller may decide:
         "status": "payée", "amount_paid": 40241, "paid_date": WHEN,
         "etag": "forged", "id": "forged", "voided": True},
        invoice_number="2019-F001",
    )
    assert errors == [], errors
    stored = fake.peek(f"invoices/{invoice['id']}")
    assert stored["status"] == "brouillon"
    assert stored["amount_paid"] == 0 and stored["paid_date"] is None
    assert stored["id"] == invoice["id"] != "forged"
    assert stored["etag"] != "forged"
    assert "voided" not in stored
    assert stored["legacy_ref"] == "OLD-1"         # whitelisted, kept
    assert stored["client_name"] == "Jean"


def test_la_liste_blanche_couvre_ce_que_les_deux_appelants_envoient():
    """Le web (routes/invoices.invoice_create) et l'import MCP
    (handlers._import_invoice_impl) construisent leur `data` clé par clé :
    chaque clé qu'ils posent doit survivre au tri, sinon un champ réel
    disparaîtrait en silence."""
    import ast

    def _data_keys(path: str, func: str) -> set[str]:
        tree = ast.parse((_ATHENA / path).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == func)
        keys: set[str] = set()
        for node in ast.walk(fn):
            if (isinstance(node, (ast.Assign, ast.AnnAssign))
                    and isinstance(getattr(node, "value", None), ast.Dict)):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                if any(isinstance(t, ast.Name) and t.id == "data"
                       for t in targets):
                    keys |= {k.value for k in node.value.keys
                             if isinstance(k, ast.Constant)}
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "data"
                    and isinstance(node.ctx, ast.Store)
                    and isinstance(node.slice, ast.Constant)):
                keys.add(node.slice.value)
        return keys

    web = _data_keys("routes/invoices.py", "invoice_create")
    mcp = _data_keys("mcp/handlers.py", "_import_invoice_impl")
    assert web and mcp
    assert web - invoice_model._CREATE_DATA_KEYS == set()
    assert (mcp | {"notes", "payment_terms"}) - invoice_model._CREATE_DATA_KEYS == set()
    for forced in ("status", "amount_paid", "paid_date", "invoice_number"):
        assert forced not in invoice_model._CREATE_DATA_KEYS


# ══════════════════════════════════════════════════════════════════════
# 6. Les littéraux recopiés restent vrais
# ══════════════════════════════════════════════════════════════════════


def test_les_noms_de_registres_sont_ceux_des_modules():
    assert invoice_model._ADMIN_TRANSACTIONS == admin_ledger_model.TRANSACTIONS_COLLECTION
    assert invoice_model._TRUST_TRANSACTIONS == trust_model.TRANSACTIONS_COLLECTION


def test_le_predicat_d_encaissement_debout_est_celui_du_grand_livre(fake):
    """_standing_admin_encaissement est RECOPIÉ de sum_invoice_receipts
    (cycle d'import) : les deux doivent compter les mêmes lignes."""
    rows = {
        "a": {"kind": "encaissement_facture", "status": "compensée", "amount": 1},
        "b": {"kind": "encaissement_facture", "status": "en_circulation", "amount": 10},
        "c": {"kind": "encaissement_facture", "status": "annulée", "amount": 100},
        "d": {"kind": "encaissement_facture", "status": "compensée",
              "reversed_by_id": "x", "amount": 1000},
        "e": {"kind": "recette_autre", "status": "compensée", "amount": 10000},
        "f": {"kind": "correction", "status": "en_circulation", "amount": 100000},
    }
    for rid, row in rows.items():
        fake.seed(f"admin_transactions/{rid}", dict(row, invoice_id=INV))
    expected = sum(r["amount"] for r in rows.values()
                   if invoice_model._standing_admin_encaissement(r))
    assert expected == 11
    assert admin_ledger_model.sum_invoice_receipts(INV) == expected


# ══════════════════════════════════════════════════════════════════════
# L'application (routes + gabarits réels)
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
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
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c
