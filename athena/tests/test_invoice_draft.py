"""Corriger un brouillon de facture (lot 3a) — ``models.invoice.update_invoice_draft``
et la page ``/factures/<id>/brouillon``.

Il n'existait AUCUN moyen de corriger un brouillon : une coquille dans ses
notes, une échéance erronée, un client qui a déménagé avant l'envoi — chaque
fois, la seule « correction » était d'annuler la facture, ce qui retire son
numéro pour toujours. La correction touche quatre choses et rien d'autre :
les notes, les conditions de paiement, la date d'échéance, et un nouvel
instantané de l'adresse de facturation lu sur la fiche ACTUELLE du client.
Jamais un montant, jamais une ligne : c'est une mise à jour PARTIELLE, et la
forme de l'écriture est épinglée.

Tout passe par le faux Firestore partagé (le client, ses transactions et la
boucle de reprise de ``transactional`` sont les vrais) ; on relit ce qui est
STOCKÉ.
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
    from models import concurrency
    from models import invoice as invoice_model
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.parties as parties_routes

from flask import Flask  # noqa: E402
from google.cloud.firestore_v1.transaction import Transaction  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 6, 15, tzinfo=UTC)
INV = "inv1"
PATH = f"invoices/{INV}"
ETAG = "aaaaaaaa-1111-4222-8333-444444444444"
RIVAL = "bbbbbbbb-1111-4222-8333-444444444444"
_MONEY = ("subtotal_fees", "subtotal_expenses", "subtotal", "gst_amount",
          "qst_amount", "total", "retainer_applied", "amount_due",
          "amount_paid", "gst_number", "qst_number", "invoice_number",
          "status", "client_id", "client_name", "date")


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _invoice(fake, **over) -> dict:
    doc = {
        **invoice_model._default_doc(),
        "id": INV, "invoice_number": "2026-F031", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "T c. L",
        "client_id": "p1", "client_name": "Jean Tremblay",
        "billing_address": {"name": "Jean Tremblay", "street": "1 rue Ancienne",
                            "unit": "", "city": "Laval", "province": "QC",
                            "postal_code": ""},
        "date": WHEN, "due_date": datetime(2026, 7, 15, tzinfo=UTC),
        "status": "brouillon",
        "subtotal_fees": 30000, "subtotal": 30000, "gst_amount": 1500,
        "qst_amount": 2993, "total": 34493, "amount_due": 34493,
        "gst_number": "123456789 RT0001", "qst_number": "1234567890 TQ0001",
        "notes": "Premier jet", "created_via": "web",
        "etag": ETAG, "created_at": WHEN, "updated_at": WHEN,
    }
    doc.update(over)
    fake.seed(PATH, doc)
    fake.seed(f"{PATH}/lineitems/li1", {
        "id": "li1", "type": "fee", "source_id": "te1", "date": WHEN,
        "description": "Rédaction", "hours": 1.0, "rate": 30000,
        "amount": 30000, "taxable": True})
    return fake.peek(PATH)


def _partie(fake, **over) -> None:
    doc = {"id": "p1", "type": "individual", "first_name": "Jean",
           "last_name": "Tremblay", "work_address_street": "450 rue Neuve",
           "work_address_city": "Montréal", "work_address_province": "Québec",
           "work_address_postal_code": "H3B 1A7"}
    doc.update(over)
    fake.seed("parties/p1", doc)


def _writes(fake) -> list:
    """Every write committed since the last reset. (A transaction that
    wrote nothing still commits — empty — as the real client does.)"""
    return [op for c in fake.commits for op in c.ops]


def _edit(changes, **kw):
    kw.setdefault("expected_etag", ETAG)
    return invoice_model.update_invoice_draft(INV, changes, **kw)


# ══════════════════════════════════════════════════════════════════════
# 1. Ce qui se corrige, et seulement cela
# ══════════════════════════════════════════════════════════════════════


def test_un_brouillon_se_corrige(fake):
    """(Capacité nouvelle : update_invoice_draft n'existait pas — l'ancienne
    seule voie était l'annulation, qui retire le numéro pour toujours.)"""
    _invoice(fake)
    doc, errors, changed = _edit({
        "notes": "  Merci de votre confiance.  ",
        "payment_terms": "Payable à réception.",
        "due_date": "2026-08-01",
    })
    assert errors == [], errors
    assert changed == ["due_date", "notes", "payment_terms"]
    stored = fake.peek(PATH)
    assert stored["notes"] == "Merci de votre confiance."
    assert stored["payment_terms"] == "Payable à réception."
    assert stored["due_date"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert stored["etag"] != ETAG and doc["etag"] == stored["etag"]
    assert stored["updated_via"] == "script"      # no request: a script
    assert stored["created_via"] == "web"          # never rewritten


def test_la_correction_est_une_mise_a_jour_partielle_aux_cles_epinglees(
    fake, monkeypatch,
):
    """La garantie est une FORME : un update() des seules clés qui changent,
    plus le tampon de provenance — jamais le set() d'un document fusionné.
    Aucun montant, aucun numéro, aucune ligne ne sont même atteignables."""
    before = _invoice(fake)
    staged = []
    real_update = Transaction.update

    def _spy(self, reference, field_updates, option=None):
        staged.append((reference.path, set(field_updates)))
        return real_update(self, reference, field_updates, option=option)

    monkeypatch.setattr(Transaction, "update", _spy)
    fake.reset_logs()
    _edit({"notes": "Autre"})
    assert staged == [(PATH, {"notes", "updated_at", "etag", "updated_via"})]
    (commit,) = fake.commits
    assert commit.ops == (("update", PATH),)
    after = fake.peek(PATH)
    for key in _MONEY:
        assert after[key] == before[key], key
    assert list(fake.peek_collection(f"{PATH}/lineitems")) == ["li1"]


@pytest.mark.parametrize("status", ["envoyée", "en_retard", "payée", "annulée"])
def test_seul_un_brouillon_se_corrige(fake, status):
    before = _invoice(fake, status=status)
    doc, errors, changed = _edit({"notes": "Tardif"})
    assert doc is None and changed == []
    assert errors == [invoice_model.draft_edit_refusal(before)]
    assert "brouillon" in errors[0] or "annulée" in errors[0]
    assert fake.peek(PATH) == before


def test_un_champ_hors_liste_est_refuse_par_son_nom(fake):
    before = _invoice(fake)
    doc, errors, _ = _edit({"notes": "x", "total": 1, "status": "payée"})
    assert doc is None
    assert "status, total" in errors[0]
    assert fake.peek(PATH) == before


def test_une_demande_vide_est_refusee(fake):
    _invoice(fake)
    assert _edit({})[1] == ["Aucune modification demandée."]


# ══════════════════════════════════════════════════════════════════════
# 2. Refuser plutôt que tronquer ou retirer
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("changes, needle", [
    ({"notes": "x" * 1501}, "1500 caractères"),
    ({"payment_terms": "y" * 501}, "500 caractères"),
    ({"payment_terms": "   "}, "ne peuvent pas être vides"),
    ({"notes": "si a < b et b > c"}, "chevrons"),
    ({"payment_terms": "<b>30 jours</b>"}, "chevrons"),
    ({"notes": 12}, "chaîne"),
    ({"due_date": ""}, "requise"),
    ({"due_date": None}, "requise"),
    ({"due_date": "15/07/2026"}, "invalide"),
    ({"due_date": "2026-06-01"}, "précède"),
])
def test_une_valeur_inacceptable_est_refusee_et_rien_n_est_ecrit(
    fake, changes, needle,
):
    before = _invoice(fake)
    doc, errors, changed = _edit(changes)
    assert doc is None and changed == []
    assert any(needle in e for e in errors), errors
    assert fake.peek(PATH) == before


def test_une_valeur_inchangee_n_est_pas_rejugee(fake):
    """Le formulaire renvoie TOUS ses champs. Un brouillon écrit avant ces
    plafonds (les notes du web allaient jusqu'à 2 000 caractères) doit
    rester corrigeable dans ses autres champs : une valeur égale à la valeur
    stockée n'est pas un changement, donc elle n'est ni jugée ni réécrite."""
    long_notes = "n" * 1800
    _invoice(fake, notes=long_notes)
    _, errors, changed = _edit({"notes": long_notes, "due_date": "2026-08-01"})
    assert errors == [] and changed == ["due_date"]
    assert fake.peek(PATH)["notes"] == long_notes
    # …but a CHANGE to it is judged: never truncated.
    _, errors, _ = _edit({"notes": long_notes + "!"}, expected_etag=None)
    assert "1500 caractères" in errors[0]


def test_une_facture_payee_n_est_pas_renvoyee_a_une_annulation_impossible():
    """Une facture payée ne s'annule pas (void_invoice_report la refuse) :
    le motif ne lui promet pas une voie qui n'existe pas."""
    refusal = invoice_model.draft_edit_refusal({"status": "payée"})
    assert "brouillon" in refusal and "annul" not in refusal
    assert "aucun paiement" in invoice_model.draft_edit_refusal(
        {"status": "envoyée"})


def test_des_notes_vides_s_effacent(fake):
    _invoice(fake)
    _, errors, changed = _edit({"notes": ""})
    assert errors == [] and changed == ["notes"]
    assert fake.peek(PATH)["notes"] == ""


def test_une_echeance_egale_a_la_date_est_permise(fake):
    _invoice(fake)
    _, errors, _ = _edit({"due_date": "2026-06-15"})
    assert errors == []


# ══════════════════════════════════════════════════════════════════════
# 3. La concurrence
# ══════════════════════════════════════════════════════════════════════


def test_un_etag_perime_est_refuse_et_rien_n_est_ecrit(fake):
    before = _invoice(fake, etag=RIVAL)
    doc, errors, changed = _edit({"notes": "Écrasement"})
    assert doc is None and changed == []
    assert errors == [concurrency.STALE_ETAG_ERROR]
    assert fake.peek(PATH) == before


def test_rejouer_une_correction_deja_faite_n_est_pas_un_conflit(fake):
    """Un double envoi, un appel rejoué : la sauvegarde a déjà eu lieu, son
    etag est périmé — mais rien ne CHANGE, donc rien ne s'écrit et rien ne
    se refuse. (Le contrôle d'etag vient APRÈS le calcul de ce qui change.)"""
    _invoice(fake)
    _edit({"notes": "Version finale"})
    written = fake.peek(PATH)
    fake.reset_logs()
    doc, errors, changed = _edit({"notes": "Version finale"})   # old ETAG
    assert errors == [] and changed == []
    assert _writes(fake) == []
    assert fake.peek(PATH) == written and doc["etag"] == written["etag"]


def test_sans_etag_attendu_rien_n_est_controle(fake):
    """Une page rendue avant l'arrivée du champ : la sauvegarde se fait
    comme avant (le dernier écrivain gagne)."""
    _invoice(fake, etag=RIVAL)
    _, errors, _ = _edit({"notes": "Sans contrôle"}, expected_etag=None)
    assert errors == []
    assert fake.peek(PATH)["notes"] == "Sans contrôle"


def test_un_envoi_concurrent_n_est_jamais_corrige_apres_coup(fake):
    """La facture part (un autre onglet la marque envoyée) entre la lecture
    de la correction et son commit : la transaction avorte, sa reprise relit
    « envoyée » et REFUSE. Une facture envoyée ne se corrige jamais, pas
    même par une course."""
    _invoice(fake)
    fired = []

    def _rival_sends_it(info):
        if not fired and any(p == PATH for _k, p in info.ops):
            fired.append(info.index)
            rival = fake.peek(PATH)
            rival.update(status="envoyée", etag=RIVAL)
            fake.external_write(PATH, rival)

    remove = fake.add_commit_hook(_rival_sends_it)
    try:
        doc, errors, _ = _edit({"notes": "Trop tard"}, expected_etag=None)
    finally:
        remove()
    assert fired, "the rival never ran — the test proves nothing"
    assert doc is None and "brouillon" in errors[0]
    stored = fake.peek(PATH)
    assert stored["status"] == "envoyée" and stored["notes"] == "Premier jet"


# ══════════════════════════════════════════════════════════════════════
# 4. L'adresse de facturation, relue sur la fiche actuelle
# ══════════════════════════════════════════════════════════════════════


def test_rafraichir_l_adresse_prend_la_fiche_actuelle_dans_la_transaction(fake):
    _invoice(fake)
    _partie(fake)
    fake.reset_logs()
    _, errors, changed = _edit({}, refresh_billing_address=True)
    assert errors == [] and changed == ["billing_address"]
    assert fake.peek(PATH)["billing_address"] == {
        "name": "Jean Tremblay", "street": "450 rue Neuve", "unit": "",
        "city": "Montréal", "province": "Québec", "postal_code": "H3B 1A7"}
    assert any(r.transactional and "parties/p1" in r.paths
               for r in fake.reads), "the client must be read IN the transaction"


def test_une_adresse_inchangee_n_ecrit_rien(fake):
    _partie(fake)
    same = invoice_model.billing_address_from(fake.peek("parties/p1"))
    _invoice(fake, billing_address=same)
    fake.reset_logs()
    _, errors, changed = _edit({}, refresh_billing_address=True)
    assert errors == [] and changed == [] and _writes(fake) == []


def test_un_client_introuvable_ne_vide_jamais_l_adresse(fake):
    before = _invoice(fake)
    doc, errors, _ = _edit({"notes": "x"}, refresh_billing_address=True)
    assert doc is None and "introuvable" in errors[0]
    assert fake.peek(PATH) == before


def test_une_facture_sans_client_n_a_rien_a_rafraichir(fake):
    before = _invoice(fake, client_id="")
    _, errors, _ = _edit({}, refresh_billing_address=True)
    assert "pas de client" in errors[0]
    assert fake.peek(PATH) == before


def test_une_panne_pendant_le_rafraichissement_n_ecrit_rien(fake, monkeypatch):
    """Échec fermé : une erreur pendant la construction de l'instantané
    n'écrit ni l'adresse ni le reste de la correction."""
    before = _invoice(fake)
    _partie(fake)

    def _boom(_partie_doc):
        raise RuntimeError("panne")

    monkeypatch.setattr(invoice_model, "billing_address_from", _boom)
    doc, errors, _ = _edit({"notes": "x"}, refresh_billing_address=True)
    assert doc is None and "Rien n'a été enregistré" in errors[0]
    assert fake.peek(PATH) == before


# ══════════════════════════════════════════════════════════════════════
# 5. Le journal
# ══════════════════════════════════════════════════════════════════════


def test_une_correction_laisse_les_noms_des_champs_jamais_leurs_valeurs(
    fake, caplog,
):
    caplog.set_level("INFO", logger="pallas.invoice")
    _invoice(fake)
    _partie(fake)
    _edit({"notes": "Secret du client"}, refresh_billing_address=True)
    (event,) = [r.json_fields for r in caplog.records
                if r.name == "pallas.invoice"]
    assert event["event"] == "invoice_draft_updated"
    assert event["fields_changed"] == ["billing_address", "notes"]
    assert event["billing_refreshed"] is True
    assert "Secret" not in str(event) and "Neuve" not in str(event)

    caplog.clear()
    _edit({"notes": "Encore"})           # the etag moved: stale
    refused = [r.json_fields for r in caplog.records
               if r.name == "pallas.invoice"][-1]
    assert (refused["operation"], refused["reason"]) == ("draft", "stale_etag")


# ══════════════════════════════════════════════════════════════════════
# 6. La page web
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(fake):
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


_ETAG_INPUT = re.compile(r'name="expected_etag" value="([^"]*)"')


def test_la_page_montre_le_brouillon_tel_qu_il_est_stocke(fake, client):
    _invoice(fake)
    html = client.get(f"/factures/{INV}/brouillon").get_data(as_text=True)
    assert _ETAG_INPUT.findall(html) == [ETAG]
    assert "Premier jet" in html
    assert 'value="2026-07-15"' in html
    assert "1 rue Ancienne" in html
    assert "ne se modifient pas" in html        # figures: void and reissue


def test_la_page_d_une_facture_envoyee_renvoie_a_sa_fiche(fake, client):
    _invoice(fake, status="envoyée")
    resp = client.get(f"/factures/{INV}/brouillon")
    assert resp.status_code == 302
    loc = urlparse(resp.location)
    assert loc.path == f"/factures/{INV}"
    assert "Seul un brouillon se modifie" in parse_qs(loc.query)["erreur"][0]


def test_enregistrer_revient_a_la_fiche(fake, client):
    _invoice(fake)
    _partie(fake)
    resp = client.post(f"/factures/{INV}/brouillon", data={
        "expected_etag": ETAG, "notes": "Revue", "due_date": "2026-07-30",
        "payment_terms": "Payable dans les 30 jours.",
        "refresh_billing_address": "on"})
    assert resp.status_code == 302
    assert urlparse(resp.location).path == f"/factures/{INV}"
    stored = fake.peek(PATH)
    assert stored["notes"] == "Revue"
    assert stored["billing_address"]["street"] == "450 rue Neuve"
    assert stored["updated_via"] == "web"


def test_une_facture_envoyee_entre_temps_renvoie_a_sa_fiche_avec_le_motif(
    fake, client,
):
    _invoice(fake, status="envoyée")
    resp = client.post(f"/factures/{INV}/brouillon", data={
        "expected_etag": ETAG, "notes": "Revue", "due_date": "2026-07-30",
        "payment_terms": "Payable."})
    assert resp.status_code == 302
    assert "erreur" in parse_qs(urlparse(resp.location).query)


def test_la_fiche_offre_la_correction_sur_un_brouillon_seulement(fake, client):
    _invoice(fake)
    link = f'href="/factures/{INV}/brouillon"'
    assert link in client.get(f"/factures/{INV}").get_data(as_text=True)
    _invoice(fake, status="envoyée")
    assert link not in client.get(f"/factures/{INV}").get_data(as_text=True)


def test_la_fiche_dit_quand_claude_a_cree_la_facture(fake, client):
    _invoice(fake)
    assert "créée par Claude" not in client.get(
        f"/factures/{INV}").get_data(as_text=True)
    _invoice(fake, created_via="mcp")
    assert "créée par Claude" in client.get(
        f"/factures/{INV}").get_data(as_text=True)
    assert "créée par Claude" in client.get(
        f"/factures/{INV}/brouillon").get_data(as_text=True)


def _compiled_css() -> str:
    return next(_ATHENA.glob("static/vendor/app.*.css")).read_text(
        encoding="utf-8")


def _absent_classes(classes: set[str], css: str) -> list[str]:
    absent = []
    for c in sorted(classes):
        needle = "." + c.replace(":", r"\:").replace("/", r"\/").replace(
            ".", r"\.")
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    return absent


def test_la_page_et_ses_ajouts_n_emploient_que_des_classes_compilees(
    fake, client,
):
    """Une classe absente de l'artefact compilé ne s'applique pas, en
    silence — et en ajouter une est un éventail de sept fichiers (CLAUDE.md,
    point 6). La page de correction et les deux ajouts de la fiche
    (le lien, la puce « créée par Claude ») réutilisent des chaînes déjà
    compilées."""
    _invoice(fake, created_via="mcp")
    pages = client.get(f"/factures/{INV}/brouillon").get_data(as_text=True)
    pages += client.post(f"/factures/{INV}/brouillon", data={
        "expected_etag": RIVAL, "notes": "x", "payment_terms": "y",
        "due_date": "2026-07-30"}).get_data(as_text=True)     # the banner
    # The page content only — never base.html's navigation or its scripts.
    def _content(page: str) -> str:
        start = page.index('<div class="max-w-3xl')
        return page[start:page.index("</form>", start)]

    body = "".join(_content(p) for p in pages.split("<!DOCTYPE")[1:])
    detail = client.get(f"/factures/{INV}").get_data(as_text=True)
    start = detail.index("Modifier le brouillon")
    body += detail[detail.rindex("<a", 0, start):start]
    chip = detail.index("créée par Claude")
    body += detail[detail.rindex("<span", 0, chip):chip]
    classes = {c for attr in re.findall(r'class="([^"]+)"', body)
               for c in attr.split() if "{" not in c}
    # Not vacuous: the form, the conflict banner and the chip were scanned.
    assert {"bg-indigo-600", "bg-amber-50", "rounded-full"} <= classes
    assert not _absent_classes(classes, _compiled_css())
