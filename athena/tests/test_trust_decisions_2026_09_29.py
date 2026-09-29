"""Le fidéicommis — les décisions de l'avocat du 2026-09-29 (D20, D21, D23,
D24), prouvées au web sur le vrai magasin et le vrai gabarit.

Chaque règle vit dans le MODÈLE (``models/trust``, ``models/fee_payment``),
si bien que le formulaire web et le connecteur refusent la même chose avec
les mêmes mots ; ici, ce que le formulaire en fait :

* **D20** (art. 56 2°) — le refus d'une facture non envoyée nomme le JURISTE
  comme celui qui atteste l'envoi, et ne dit jamais comment passer outre.
* **D21** — les fonds en fidéicommis d'un client n'acquittent jamais la
  facture d'un AUTRE client du dossier : un refus en ligne au formulaire,
  sans dérogation, là où un bandeau disait après coup « vérifiez que ce
  client a autorisé ce paiement », les honoraires déjà sortis.
* **D23** (art. 58) — le bénéficiaire d'un paiement d'honoraires est
  l'avocat ou son cabinet, tels que les nomme le profil du cabinet : le
  formulaire offre ces deux noms, jamais un texte libre, et le modèle refuse
  tout autre nom.
* **D24** — l'objet « virement inter-dossiers » n'appartient qu'à l'écran de
  virement à deux volets (retiré du formulaire, refusé par la création
  publique), et l'objet et le sens concordent pour tout appelant ; les
  écritures déjà inscrites restent, contre-passables (tests/test_trust_rules,
  tests/test_verify_trust_integrity).

Chaque test marqué « régression » échoue sur l'ancien code.
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
    from models import fee_payment, trust
    from models import settings as settings_model  # noqa: F401 — faked below
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
LAWYER = "Me Jason Poirier Lavoie"
FIRM = "Poirier Lavoie, avocat"


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _freeze(monkeypatch, iso: str = "2026-09-20T16:00:00+00:00") -> None:
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)


@pytest.fixture
def fake(monkeypatch):
    _freeze(monkeypatch)
    f = install(monkeypatch, *_fake_modules())
    f.seed("settings/cabinet", {"nom": LAWYER, "organisation": FIRM})
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0,
        "etag": "t0",
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2026-001", "title": "T c. X",
        "status": "actif", "client_ids": ["c1", "c2"],
        "clients": [{"id": "c1", "name": "Jean Tremblay"},
                    {"id": "c2", "name": "Marie Roy"}],
        "trust_balance": 0, "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
    })
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "a0",
    })
    for iid, number, status in (("inv1", "2026-F040", "envoyée"),
                                ("inv3", "2026-F042", "brouillon")):
        f.seed(f"invoices/{iid}", {
            "id": iid, "invoice_number": number, "dossier_id": "dos1",
            "dossier_file_number": "2026-001", "client_id": "c1",
            "client_name": "Jean Tremblay", "status": status, "total": 100000,
            "retainer_applied": 0, "amount_due": 100000, "amount_paid": 0,
            "paid_date": None, "etag": f"{iid}-e0",
        })
    for cid, name in (("c1", "Jean Tremblay"), ("c2", "Marie Roy")):
        receipt, errs = trust.create_transaction(_entry(
            direction="recette", purpose="dépôt_client", amount=200000,
            counterparty=name, client_id=cid, date=_d(2026, 9, 2)))
        assert errs == [], errs
        _, errs = trust.clear_transaction(receipt["id"], _d(2026, 9, 2))
        assert errs == [], errs
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
        jsattr=lambda v: v,
    )
    for bp in (trust_routes.trust_bp, invoices_routes.invoices_bp,
               admin_ledger_routes.admin_bp, dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _entry(**over) -> dict:
    d = {
        "account_id": "acc1", "direction": "déboursé", "amount": 10000,
        "purpose": "déboursé_tiers", "method": "chèque", "counterparty": "Huissier X",
        "dossier_id": "dos1", "client_id": "c1", "date": _d(2026, 9, 10),
        "description": "", "reference": "",
    }
    d.update(over)
    return d


def _form(**over) -> dict:
    f = {
        "account_id": "acc1", "direction": "déboursé", "amount": "100,00",
        "purpose": "déboursé_tiers", "method": "chèque",
        "counterparty": "Huissier X", "dossier_id": "dos1", "client_id": "c1",
        "date": "2026-09-10", "reference": "", "description": "",
    }
    f.update(over)
    return f


def _fee_form(**over) -> dict:
    return _form(**{
        "purpose": "virement_honoraires", "amount": "600,00",
        "counterparty": FIRM, "invoice_number": "2026-F040",
        "admin_account_id": "ops1", **over,
    })


def _registers(fake) -> tuple:
    return (fake.peek_collection("trust_transactions"),
            fake.peek_collection("admin_transactions"),
            fake.peek_collection("invoices"),
            fake.peek("dossiers/dos1"))


def _html(resp) -> str:
    return resp.get_data(as_text=True).replace("&#39;", "'")


# ══════════════════════════════════════════════════════════════════════
# D24 — le virement inter-dossiers n'a qu'un chemin ; l'objet dit le sens
# ══════════════════════════════════════════════════════════════════════


def test_d24_le_formulaire_n_offre_plus_le_virement_inter_dossiers(client):
    """Régression — le sélecteur « Objet » offrait « Virement
    inter-dossiers » : chaque écriture ainsi faite était UN volet isolé, le
    solde d'un seul client déplacé sans contre-volet. Il offre tous les
    autres objets, sauf la correction, et renvoie à l'écran de virement."""
    html = _html(client.get("/fideicommis/nouvelle"))
    select = html[html.index('name="purpose"'):]
    select = select[:select.index("</select>")]
    offered = re.findall(r'<option value="([^"]+)"', select)
    assert "virement_inter_dossiers" not in offered
    assert "correction" not in offered
    assert set(offered) == set(trust.VALID_PURPOSES) - {
        trust.REVERSAL_PURPOSE, trust.TRANSFER_PURPOSE}
    assert ("s'inscrit par l'écran <a href=\"/fideicommis/virement\"" in html
            and "« Virement inter-dossiers »</a>" in html)


def test_d24_la_creation_publique_refuse_le_virement_inter_dossiers(fake, client):
    """Régression — refusé par le MODÈLE, quel que soit l'appelant (un
    formulaire forgé, un script) : rien n'est écrit, et le refus nomme
    l'écran qui écrit les deux volets."""
    before = _registers(fake)
    report: dict = {}
    entry, errs = trust.create_transaction(
        _entry(purpose="virement_inter_dossiers"), _report_out=report)
    assert entry is None
    assert errs == [trust._ABORT_MESSAGES["virement_inter_dossiers_réservé"]]
    assert report.get("reason") == "virement_inter_dossiers_réservé"
    resp = client.post("/fideicommis/", data=_form(purpose="virement_inter_dossiers"))
    assert resp.status_code == 400
    assert "Virement inter-dossiers" in _html(resp)
    assert trust._ABORT_MESSAGES["virement_inter_dossiers_réservé"] in _html(resp)
    assert _registers(fake) == before


def test_d24_le_virement_a_deux_volets_reste_le_chemin(fake):
    """L'écran de virement, lui, écrit ses deux volets — l'objet ne lui est
    pas retiré."""
    legs, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos1", "c2", 5000, "réaffectation", "virement", "")
    assert errs == [], errs
    transfers = [t for t in fake.peek_collection("trust_transactions").values()
                 if t["purpose"] == trust.TRANSFER_PURPOSE]
    assert len(transfers) == 2
    assert {t["direction"] for t in transfers} == {"recette", "déboursé"}


@pytest.mark.parametrize("purpose, direction", [
    ("dépôt_client", "déboursé"), ("avance_honoraires", "déboursé"),
    ("remise_client", "recette"), ("déboursé_tiers", "recette"),
])
def test_d24_un_objet_qui_contredit_le_sens_est_refuse_au_web(fake, client, purpose, direction):
    """Régression — le formulaire web acceptait tout couple : « Dépôt du
    client » en déboursé imprimait au registre de l'art. 38 une ligne qui dit
    le contraire du mouvement. Le modèle refuse désormais le couple pour
    tout appelant ; le formulaire le dit en ligne, rien n'est écrit."""
    before = _registers(fake)
    resp = client.post("/fideicommis/", data=_form(purpose=purpose, direction=direction))
    assert resp.status_code == 400
    assert trust._ABORT_MESSAGES["objet_sens_incohérent"] in _html(resp)
    assert _registers(fake) == before


@pytest.mark.parametrize("purpose", ["règlement", "autre"])
@pytest.mark.parametrize("direction", ["recette", "déboursé"])
def test_d24_un_objet_ambigu_va_dans_les_deux_sens(fake, client, purpose, direction):
    resp = client.post("/fideicommis/", data=_form(purpose=purpose, direction=direction))
    assert resp.status_code == 302, _html(resp)


def test_d24_le_refus_nomme_les_quatre_objets_depuis_la_carte_du_modele():
    text = trust._ABORT_MESSAGES["objet_sens_incohérent"]
    for purpose in trust.PURPOSE_DIRECTIONS:
        assert f"« {trust.PURPOSE_LABELS[purpose]} »" in text, purpose


def test_d24_le_formulaire_porte_la_carte_du_modele_pour_regler_le_sens(client):
    """Le formulaire règle le sens quand l'objet est choisi — sur la carte
    DU MODÈLE (plus le paiement d'honoraires, toujours un déboursé), portée
    dans un bloc de données non exécutable, jamais recopiée à la main."""
    import json

    html = client.get("/fideicommis/nouvelle").get_data(as_text=True)
    block = re.search(
        r'<script type="application/json" id="trust-purpose-directions">(.*?)</script>',
        html, re.S)
    assert block is not None
    assert json.loads(block.group(1)) == {
        **trust.PURPOSE_DIRECTIONS, trust.FEE_PAYMENT_PURPOSE: "déboursé"}
    assert "$watch('purpose'" in html


# ══════════════════════════════════════════════════════════════════════
# D23 — le bénéficiaire : l'avocat ou son cabinet
# ══════════════════════════════════════════════════════════════════════


def test_d23_le_formulaire_offre_les_deux_noms_du_profil(client):
    """Régression — le bénéficiaire d'un paiement d'honoraires était le
    champ libre de toute écriture. Sur un paiement d'honoraires, ce champ
    est désactivé (ni soumis ni exigé) et un sélecteur offre les deux noms
    du profil du cabinet, le cabinet d'abord."""
    html = _html(client.get("/fideicommis/nouvelle"))
    i = html.index("Bénéficiaire du paiement d'honoraires")
    block = html[i:html.index("</select>", i)]
    assert re.findall(r'<option value="([^"]+)"', block) == [FIRM, LAWYER]
    assert ':disabled="purpose !== \'virement_honoraires\'"' in block
    free = html[html.index('name="counterparty" required'):]
    free = free[:free.index(">")]
    assert ':disabled="purpose === \'virement_honoraires\'"' in free


def test_d23_un_autre_beneficiaire_est_refuse_au_formulaire(fake, client):
    """Un formulaire forgé qui nomme un autre bénéficiaire : refusé par le
    modèle, rendu en ligne, rien d'écrit."""
    before = _registers(fake)
    resp = client.post("/fideicommis/", data=_fee_form(counterparty="Jean Tremblay"))
    assert resp.status_code == 400
    html = _html(resp)
    assert "art. 58" in html and f"« {FIRM} » ou « {LAWYER} »" in html
    assert _registers(fake) == before


def test_d23_le_beneficiaire_choisi_est_inscrit_comme_le_profil_l_ecrit(fake, client):
    resp = client.post("/fideicommis/", data=_fee_form(
        counterparty=LAWYER.upper(), amount="100,00"))
    assert resp.status_code == 302, _html(resp)
    fees = [t for t in fake.peek_collection("trust_transactions").values()
            if t["purpose"] == trust.FEE_PAYMENT_PURPOSE]
    assert [t["counterparty"] for t in fees] == [LAWYER]


def test_d23_un_profil_sans_nom_le_dit_au_formulaire(fake, client):
    fake.seed("settings/cabinet", {"nom": "", "organisation": ""})
    html = _html(client.get("/fideicommis/nouvelle"))
    assert "ne nomme ni l'avocat ni le cabinet" in html
    resp = client.post("/fideicommis/", data=_fee_form())
    assert resp.status_code == 400
    assert fee_payment._MESSAGES["bénéficiaires_honoraires_inconnus"] in _html(resp)


# ══════════════════════════════════════════════════════════════════════
# D21 — la facture d'un autre client du dossier : un refus en ligne
# ══════════════════════════════════════════════════════════════════════


def test_d21_la_facture_d_un_autre_client_est_refusee_en_ligne(fake, client):
    """Régression — inv1 est adressée à c1 ; le formulaire tirait les fonds
    COMPENSÉS de c2 pour elle, puis la fiche montrait un bandeau « vérifiez
    que ce client a autorisé ce paiement ». Désormais : le formulaire se
    réaffiche avec le refus, sans nom ni montant, et RIEN n'est inscrit —
    ni au fidéicommis, ni au compte d'administration, ni sur la facture."""
    before = _registers(fake)
    resp = client.post("/fideicommis/", data=_fee_form(client_id="c2"))
    assert resp.status_code == 400
    assert "Location" not in resp.headers
    html = _html(resp)
    message = trust._ABORT_MESSAGES["facture_autre_client"]
    assert message in html
    errors = html[html.index("bg-red-50"):]
    errors = errors[:errors.index("</div>")]
    for word in ("Jean Tremblay", "Marie Roy", "600,00"):
        assert word not in errors, word
    assert _registers(fake) == before


def test_d21_le_client_facture_paie_toujours(fake, client):
    resp = client.post("/fideicommis/", data=_fee_form(client_id="c1"))
    assert resp.status_code == 302, _html(resp)
    assert "avertissement" not in resp.headers["Location"]
    assert fake.peek("invoices/inv1")["amount_paid"] == 60000


# ══════════════════════════════════════════════════════════════════════
# D20 — la facture non envoyée : le juriste atteste, et rien ne dit comment
#        passer outre
# ══════════════════════════════════════════════════════════════════════


def test_d20_le_refus_d_une_facture_non_envoyee_au_formulaire(fake, client):
    before = _registers(fake)
    resp = client.post("/fideicommis/", data=_fee_form(invoice_number="2026-F042"))
    assert resp.status_code == 400
    html = _html(resp)
    assert trust._ABORT_MESSAGES["facture_non_émise"] in html
    assert "envoyée par le juriste" in html
    assert "update_invoice" not in html and "romouv" not in html
    assert _registers(fake) == before


# ══════════════════════════════════════════════════════════════════════
# Le balisage n'emploie que des classes compilées
# ══════════════════════════════════════════════════════════════════════


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def test_le_formulaire_n_emploie_que_des_classes_compilees(fake, client):
    """A class absent from the compiled artifact silently does not apply
    (CLAUDE.md item 6) — the form, with a refusal box and without a payee."""
    pages = [client.get("/fideicommis/nouvelle").get_data(as_text=True),
             client.post("/fideicommis/", data=_fee_form(client_id="c2")).get_data(as_text=True)]
    fake.seed("settings/cabinet", {"nom": "", "organisation": ""})
    pages.append(client.get("/fideicommis/nouvelle").get_data(as_text=True))
    classes = set()
    for html in pages:
        form = html[html.index("<form"):html.index("</form>")]
        classes |= {c for block in re.findall(r'class="([^"]+)"', form)
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
