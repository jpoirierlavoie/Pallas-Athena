"""Épingles d'intégration du module fidéicommis — le miroir de la section
« Template pins » de test_admin_integration.py.

Phase 4 de la consolidation « Comptabilité » (2026-08-15) : les correctifs de
parité que l'administration portait déjà (revue 2026-08-13) sont rétroportés
au fidéicommis, chacun avec son épingle — la consigne « a fix on one side
should be mirrored on the other » devient exécutable au lieu de vivre en
commentaire. Le pin anti-fonctions-fléchées n'est PAS copié en balayage de
tout templates/trust/ : reconciliation_worksheet.html utilise `el =>`
légitimement — on épingle les gabarits touchés seulement.
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
    import routes.trust as rt
    import routes.admin_ledger as ra

from flask import Flask  # noqa: E402


def _template(name: str) -> str:
    path = os.path.join(_ATHENA, "templates", name)
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_le_select_de_compte_du_journal_porte_hx_include():
    """Sans hx-include, changer de compte perdait silencieusement les filtres
    actifs (statut/sens/période) — le bogue corrigé côté administration le
    2026-08-13 et jamais rétroporté jusqu'ici. Même épingle que
    test_admin_integration (le pin admin cherche la ligne du select compte et
    exige hx-include dessus)."""
    src = _template("trust/list.html")
    for line in src.splitlines():
        if 'name="account_id"' in line and "hx-get" in line:
            assert "hx-include" in line
            break
    else:
        raise AssertionError("account select not found")


def test_le_type_de_compte_est_fige_en_edition():
    """update_account écarte account_type de sa whitelist des deux côtés ;
    le form trust affichait pourtant un select inconditionnel — un mensonge
    d'interface (le changement semblait enregistré et ne l'était jamais).
    Miroir du form admin : input disabled en édition, select à la création
    seulement."""
    src = _template("trust/account_form.html")
    assert "{% if account and account.id %}" in src
    assert "disabled" in src
    # Le select de création ne porte plus de branche « selected » d'édition.
    assert 'account.account_type == t' not in src


def test_comptes_et_conciliations_sont_atteignables_sur_mobile():
    """« Comptes » et « Conciliations » étaient hidden md:inline-flex sur les
    DEUX journaux : au téléphone, ces écrans n'existaient pas hors URL tapée.
    Le hub règle l'accès de nav ; la visibilité locale reste due — et le
    conteneur porte flex-wrap pour que 375 px fasse passer le groupe sous le
    titre au lieu de déborder."""
    for name in ("trust/list.html", "administration/list.html"):
        src = _template(name)
        assert "hidden md:inline-flex" not in src, name
        assert "flex flex-wrap items-center justify-between" in src, name


def test_rows_partial_reemits_export_et_header_oob():
    """Miroir de test_rows_partial_reemits_the_export_links_oob (admin) :
    l'export ET l'en-tête se ré-émettent hors bande — sans le second, un
    changement de compte laissait les soldes de l'ANCIEN compte au-dessus du
    registre du nouveau (des chiffres d'argent faux à côté d'un livre de
    compte). Hors des branches lignes/vide, gardé sur HX-Request pour qu'un
    rendu pleine page n'émette jamais l'id deux fois."""
    rows = _template("trust/_transaction_rows.html")
    assert 'hx-swap-oob="true"' in rows
    assert 'id="trust-export"' in rows
    assert 'id="trust-header"' in rows
    assert "request.headers.get('HX-Request')" in rows

    page = _template("trust/list.html")
    assert 'id="trust-export"' in page
    assert 'id="trust-header"' in page
    assert 'hx-target="#trust-rows"' in page


_ACC = {
    "id": "ta1", "name": "Compte général", "account_type": "général",
    "status": "actif", "institution": "BNC", "account_number_last4": "9876",
    "book_balance": 250000, "bank_balance": 200000, "created_at": None,
    "notes": "", "transit": "12345",
}


@pytest.fixture()
def web_rendu(monkeypatch):
    """Rendu réel du journal trust. trust_bp ET admin_bp : trust/form.html
    porte le select admin_account_id, et un url_for('admin_ledger.…') dans
    l'arbre de rendu exige le blueprint enregistré."""
    from utils.format_fr import format_cents_fr
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.jinja_env.filters["cents_fr"] = format_cents_fr
    app.register_blueprint(rt.trust_bp)
    app.register_blueprint(ra.admin_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _journal_lectures(monkeypatch):
    monkeypatch.setattr(rt.trust, "list_accounts", lambda status=None: [dict(_ACC)])
    monkeypatch.setattr(rt.trust, "list_outstanding", lambda aid, as_of=None: [])
    monkeypatch.setattr(rt.trust, "list_in_transit", lambda aid, as_of=None: [])
    monkeypatch.setattr(rt.trust, "list_reconciliations", lambda aid=None: [])
    monkeypatch.setattr(rt.trust, "list_transactions_page",
                        lambda aid, cursor=None, limit=15: ([], None))


def test_rendu_pleine_page_un_seul_header(web_rendu, monkeypatch):
    """Le rendu complet n'émet l'id qu'une fois — jamais le jumeau OOB."""
    _journal_lectures(monkeypatch)
    html = web_rendu.get("/fideicommis/").get_data(as_text=True)
    assert html.count('id="trust-header"') == 1
    assert html.count('id="trust-export"') == 1
    assert "Solde aux livres" in html


def test_echange_htmx_reemet_header_et_export(web_rendu, monkeypatch):
    """La requête HTMX (un changement de filtre OU de compte) porte les DEUX
    jumeaux OOB : les soldes affichés suivent le compte du registre."""
    _journal_lectures(monkeypatch)
    html = web_rendu.get("/fideicommis/",
                         headers={"HX-Request": "true"}).get_data(as_text=True)
    assert html.count('id="trust-header"') == 1
    assert 'hx-swap-oob="true"' in html
    assert html.count('id="trust-export"') == 1
    assert "Solde aux livres" in html   # les cartes voyagent avec l'échange
