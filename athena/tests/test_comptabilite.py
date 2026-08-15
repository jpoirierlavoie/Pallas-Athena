"""Hub « Comptabilité » — le composeur de présentation des deux comptabilités.

Le hub est LECTURE SEULE pour toujours et compose les deux instantanés
EXISTANTS (get_firm_trust_snapshot — sous contrat outputSchema MCP, jamais
modifié — et get_firm_admin_snapshot). Ce qu'on épingle ici :

- le rendu RÉEL (la leçon du 2026-08-13 : une épingle de source avait laissé
  passer un lien cassé) avec les TROIS blueprints enregistrés — le gabarit
  porte des url_for inconditionnels des deux espaces de noms, et une fixture
  qui n'en enregistre que deux lèverait BuildError ;
- le fail-closed PAR SECTION : une panne de lecture d'un côté rend un panneau
  « indisponibles » SANS bouton de création (un état vide sur une panne
  inviterait à recréer un compte en double) pendant que l'autre section rend
  normalement — jamais un 500 de page ;
- le libellé de solde PAR LIGNE (« Solde aux livres » / « Solde » /
  « Solde dû ») — trois grandeurs incommensurables, jamais une colonne
  commune, jamais un total combiné.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.comptabilite as rc
    import routes.trust as rt
    import routes.admin_ledger as ra

from flask import Flask  # noqa: E402


_TRUST_SNAP = {
    "accounts": [
        {
            "id": "ta1", "name": "Compte général en fidéicommis",
            "account_type": "général", "status": "actif",
            "institution": "BNC", "account_number_last4": "9876",
            "book_balance": 250000, "bank_balance": 200000,
            "last_reconciliation_date": None, "never_reconciled": False,
            "reconciliation_overdue": False,
        },
    ],
    "total_held_cents": 250000,
    "reconciliation_overdue": False,
}

_ADMIN_SNAP = {
    "accounts": [
        {
            "id": "aa1", "name": "Opérations", "account_type": "opérations",
            "status": "actif", "institution": "BNC",
            "account_number_last4": "1234", "ledger_balance": 130000,
            "display_balance": 130000, "balance_label": "Solde",
            "last_reconciliation_date": None, "never_reconciled": True,
            "reconciliation_overdue": False,
        },
        {
            "id": "aa2", "name": "Carte corporative",
            "account_type": "carte_crédit", "status": "actif",
            "institution": "BNC", "account_number_last4": "5678",
            "ledger_balance": -45000,
            "display_balance": 45000, "balance_label": "Solde dû",
            "last_reconciliation_date": None, "never_reconciled": False,
            "reconciliation_overdue": True,
        },
    ],
    "reconciliation_overdue": True,
}


@pytest.fixture()
def web_rendu(monkeypatch):
    """Rendu réel avec les TROIS blueprints — le gabarit du hub émet des
    url_for('trust.…') ET url_for('admin_ledger.…') inconditionnels ;
    contraste voulu avec les smoke admin, qui n'enregistrent que admin_bp
    (leurs liens croisés sont gardés par des {% if %})."""
    from utils.format_fr import format_cents_fr
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.jinja_env.filters["cents_fr"] = format_cents_fr
    app.register_blueprint(rc.comptabilite_bp)
    app.register_blueprint(rt.trust_bp)
    app.register_blueprint(ra.admin_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _snapshots(monkeypatch, trust_snap=None, admin_snap=None):
    monkeypatch.setattr(
        rc.trust, "get_firm_trust_snapshot",
        (lambda: dict(trust_snap)) if trust_snap is not None else _boom,
    )
    monkeypatch.setattr(
        rc.al, "get_firm_admin_snapshot",
        (lambda: dict(admin_snap)) if admin_snap is not None else _boom,
    )


def _boom():
    raise RuntimeError("firestore indisponible")


# ── Rendu nominal ──────────────────────────────────────────────────────────


def test_rendu_nominal_deux_sections(web_rendu, monkeypatch):
    _snapshots(monkeypatch, _TRUST_SNAP, _ADMIN_SNAP)
    html = web_rendu.get("/comptabilite/").get_data(as_text=True)

    from utils.format_fr import format_cents_fr

    assert "Fidéicommis" in html and "Administration" in html
    assert "Compte général en fidéicommis" in html
    assert "Opérations" in html and "Carte corporative" in html
    # Le libellé du solde voyage par ligne — trois grandeurs distinctes.
    assert "Solde aux livres" in html
    assert "Solde dû" in html
    # Le solde de la carte est le chiffre AFFICHABLE (signe inversé) ; les
    # montants se comparent via format_cents_fr (le millier est un NBSP).
    assert format_cents_fr(45000) in html
    assert format_cents_fr(250000) in html


def test_les_actions_menent_aux_ecrans_des_modules(web_rendu, monkeypatch):
    _snapshots(monkeypatch, _TRUST_SNAP, _ADMIN_SNAP)
    html = web_rendu.get("/comptabilite/").get_data(as_text=True)

    assert "/fideicommis/?account_id=ta1" in html
    assert "/fideicommis/comptes/ta1" in html
    assert "/administration/?account_id=aa2" in html
    assert "/administration/comptes/aa1" in html
    # Ouvrir un compte = le formulaire du module, jamais un écran du hub.
    assert "/fideicommis/comptes/nouveau" in html
    assert "/administration/comptes/nouveau" in html
    assert "/fideicommis/conciliations/nouvelle" in html
    assert "/administration/conciliations/nouvelle" in html


def test_badges_type_ferme_et_conciliation(web_rendu, monkeypatch):
    trust_snap = dict(_TRUST_SNAP)
    trust_snap["accounts"] = [dict(_TRUST_SNAP["accounts"][0],
                                   status="fermé",
                                   reconciliation_overdue=True)]
    _snapshots(monkeypatch, trust_snap, _ADMIN_SNAP)
    html = web_rendu.get("/comptabilite/").get_data(as_text=True)

    from markupsafe import escape

    assert "Général" in html                       # badge de type trust
    assert str(escape("Compte d'opérations")) in html  # badge admin (apostrophe échappée)
    assert "Carte de crédit" in html
    assert "Fermé" in html
    assert "Conciliation en retard" in html
    assert "Jamais concilié" in html         # aa1 : never_reconciled sans retard


def test_aucun_total_combine(web_rendu, monkeypatch):
    """2 500,00 + 1 300,00 − 450,00 : additionner l'argent des clients à
    l'encaisse du cabinet et à une dette de carte au signe inversé produirait
    un chiffre juridiquement trompeur — le hub n'affiche JAMAIS de total."""
    _snapshots(monkeypatch, _TRUST_SNAP, _ADMIN_SNAP)
    html = web_rendu.get("/comptabilite/").get_data(as_text=True)

    for combined in ("4 250,00", "3 350,00", "3 800,00", "Total"):
        assert combined not in html


# ── Fail-closed par section ────────────────────────────────────────────────


def test_panne_trust_la_section_admin_rend_encore(web_rendu, monkeypatch):
    _snapshots(monkeypatch, trust_snap=None, admin_snap=_ADMIN_SNAP)
    reponse = web_rendu.get("/comptabilite/")
    html = reponse.get_data(as_text=True)

    assert reponse.status_code == 200        # jamais un 500 de page
    assert "Les comptes en fidéicommis sont temporairement indisponibles" in html
    assert "Carte corporative" in html       # l'autre section vit
    # Une panne ne se lit JAMAIS « aucun compte » : ni état vide, ni bouton
    # de création côté fidéicommis (recréer un compte en double serait pire
    # que la panne).
    assert "Aucun compte configuré." not in html
    assert "/fideicommis/comptes/nouveau" not in html
    assert "/administration/comptes/nouveau" in html


def test_panne_admin_le_miroir(web_rendu, monkeypatch):
    _snapshots(monkeypatch, trust_snap=_TRUST_SNAP, admin_snap=None)
    reponse = web_rendu.get("/comptabilite/")
    html = reponse.get_data(as_text=True)

    assert reponse.status_code == 200
    assert "Les comptes d'administration sont temporairement indisponibles" in html
    assert "Compte général en fidéicommis" in html
    assert "Aucun compte configuré." not in html
    assert "/administration/comptes/nouveau" not in html
    assert "/fideicommis/comptes/nouveau" in html


def test_etats_vides_avec_lecture_reussie(web_rendu, monkeypatch):
    """L'état vide ne se rend QUE si la lecture a réussi — et alors le
    bouton « Nouveau compte » est légitime dans les deux sections."""
    _snapshots(monkeypatch, {"accounts": []}, {"accounts": []})
    html = web_rendu.get("/comptabilite/").get_data(as_text=True)

    assert html.count("Aucun compte configuré.") == 2
    assert "/fideicommis/comptes/nouveau" in html
    assert "/administration/comptes/nouveau" in html
    assert "indisponibles" not in html


# ── Épingles de source ─────────────────────────────────────────────────────


def _source() -> str:
    path = os.path.join(_ATHENA, "templates", "comptabilite", "index.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_le_gabarit_ne_code_aucun_chemin_en_dur():
    """Tout lien passe par url_for — un préfixe en dur survivrait à un
    renommage de blueprint en pointant dans le vide."""
    src = _source()
    assert "/fideicommis" not in src
    assert "/administration" not in src


def test_le_gabarit_est_statique_et_sans_fleches():
    """Zéro HTMX, zéro script, zéro fonction fléchée (la leçon du parsing
    naïf d'attributs, test_no_arrow_functions côté admin) : le hub v1 est un
    rendu serveur complet — aucun échec de swap possible."""
    src = _source()
    assert "=>" not in src
    assert "<script" not in src
    assert "hx-" not in src


def test_la_route_est_en_lecture_seule():
    """Le hub ne déclare aucune route POST — « ouvrir, fermer, concilier »
    vivent sur les écrans des modules, où leurs gardes vivent (solde nul
    requis côté trust, fermeture libre côté admin)."""
    app = Flask(__name__)
    app.register_blueprint(rc.comptabilite_bp)
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("comptabilite."):
            assert rule.methods <= {"GET", "HEAD", "OPTIONS"}, rule
