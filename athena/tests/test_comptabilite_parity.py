"""Watchdog de parité fidéicommis ↔ administration + invariants du hub.

Les deux modules comptables sont des JUMEAUX DÉLIBÉRÉS — 12 paires de
gabarits copiés, jamais partagés (doctrine « the two modules must be free to
diverge », models/admin_ledger.py) — et la consigne qui accompagne cette
duplication (« a fix on one side should be mirrored on the other ») vivait en
commentaire, inexécutable. Ce fichier la rend exécutable : un TEST, pas un
script, parce que la porte pytest de Cloud Build l'exécute à chaque
déploiement alors qu'un scripts/*.py ne tourne jamais tout seul (la leçon
check_config.py du 2026-08-11).

Le watchdog épingle la parité de CÂBLAGE (mécanismes HTMX/OOB), jamais le
CONTENU — les divergences de contenu (colonne Solde conditionnelle, libellés
carte, résurrection du worksheet) sont le produit, pas un accident.
"""

import os
import re
import sys
from unittest import mock

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.admin_ledger as _ra
    import routes.trust as _rt


def _template(name: str) -> str:
    path = os.path.join(_ATHENA, "templates", name)
    with open(path, encoding="utf-8") as f:
        return f.read()


_PAIRES = {"trust": "trust", "admin": "administration"}
_IDS = {"trust": "trust", "admin": "admin"}


# ── Parité de câblage HTMX/OOB — vaut dans les DEUX jumeaux ────────────────


def test_les_deux_rows_partials_reemettent_export_et_header_oob():
    """Le partial des rangées ré-émet l'export ET l'en-tête hors bande,
    hors des branches lignes/vide, gardé sur HX-Request. Un seul côté
    corrigé = l'autre affiche des chiffres périmés au-dessus de son
    registre."""
    for cle, rep in _PAIRES.items():
        src = _template(f"{rep}/_transaction_rows.html")
        prefix = _IDS[cle]
        # L'attribut OOB est lié AU MÊME TAG que l'id (revue 2026-08-15 :
        # quatre assertions de sous-chaînes indépendantes laissaient passer
        # un header ré-émis SANS hx-swap-oob — htmx l'aurait alors injecté
        # EN LIGNE dans #rows, id dupliqué, en-tête périmé au-dessus).
        assert re.search(
            rf'<div id="{prefix}-export"[^>]*hx-swap-oob="true"', src
        ), rep
        assert re.search(
            rf'<div id="{prefix}-header"[^>]*hx-swap-oob="true"', src
        ), rep
        assert "request.headers.get('HX-Request')" in src, rep


def test_les_deux_selects_de_compte_portent_hx_include():
    """Sans hx-include, changer de compte perd les filtres actifs en
    silence (revue 2026-08-13 ; rétroporté au trust le 2026-08-15)."""
    for cle, rep in _PAIRES.items():
        src = _template(f"{rep}/list.html")
        for line in src.splitlines():
            if 'name="account_id"' in line and "hx-get" in line:
                assert "hx-include" in line, rep
                break
        else:
            raise AssertionError(f"account select not found: {rep}")


def test_les_deux_pages_ancrent_header_et_export():
    for cle, rep in _PAIRES.items():
        src = _template(f"{rep}/list.html")
        prefix = _IDS[cle]
        assert f'id="{prefix}-header"' in src, rep
        assert f'id="{prefix}-export"' in src, rep
        assert f'hx-target="#{prefix}-rows"' in src, rep


def test_les_deux_forms_de_compte_figent_le_type_en_edition():
    """update_account écarte account_type des deux côtés — le formulaire ne
    doit le proposer qu'à la création (le select inconditionnel était un
    mensonge d'interface)."""
    for rep in _PAIRES.values():
        src = _template(f"{rep}/account_form.html")
        assert "{% if account and account.id %}" in src, rep
        assert "disabled" in src, rep


def test_les_deux_details_de_compte_preselectionnent_la_conciliation():
    for cle, rep in _PAIRES.items():
        src = _template(f"{rep}/account_detail.html")
        assert "reconciliation_new', account_id=account.id" in src, rep


def test_les_deux_labels_publient_les_memes_cles_jinja():
    """Les deux _labels() partagent leur contrat de clés communes — la garde
    contre une divergence qui casserait un futur gabarit s'appuyant sur les
    clés partagées (account_type_labels…). Chaque côté peut en publier PLUS
    (balance_labels, category_labels… côté admin) — jamais moins que le
    tronc commun."""
    tronc = {
        "account_type_labels", "account_status_labels", "valid_account_types",
        "reconciliation_status_labels", "direction_labels", "method_labels",
        "tx_status_labels", "valid_methods", "valid_tx_statuses", "today",
    }
    trust_keys = set(_rt._labels().keys())
    admin_keys = set(_ra._labels().keys())
    assert tronc <= trust_keys, tronc - trust_keys
    assert tronc <= admin_keys, tronc - admin_keys


# ── Invariants du hub et de la nav ─────────────────────────────────────────


def test_base_html_ne_pointe_que_le_hub():
    """La nav passe par /comptabilite (×2, barre latérale + menu « Plus ») ;
    plus aucun /administration ni /fideicommis en dur dans base.html."""
    src = _template("base.html")
    assert src.count('href="/comptabilite"') == 2
    assert "/administration" not in src
    assert "/fideicommis" not in src


def test_le_hub_reste_sans_chemin_en_dur_et_sans_script():
    """Le hub est un composeur de présentation : tout lien par url_for,
    zéro script, zéro HTMX, zéro fonction fléchée."""
    src = _template("comptabilite/index.html")
    assert "/fideicommis" not in src
    assert "/administration" not in src
    assert "<script" not in src
    assert "hx-" not in src
    assert "=>" not in src


def test_le_hub_n_a_aucune_route_d_ecriture():
    """LECTURE SEULE pour toujours — un POST au hub serait un chemin
    d'écriture hors des modules, la catégorie exacte que la consolidation
    s'interdit."""
    path = os.path.join(_ATHENA, "routes", "comptabilite.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # Aucun methods= : chaque @route reste sur le GET par défaut de Flask
    # (la docstring du module peut citer le mot POST — on épingle le CODE).
    assert not re.search(r"methods\s*=", src)
    # Et aucune écriture modèle : le hub ne nomme aucun create_/update_/
    # delete_/reverse_ des deux modèles.
    for verbe in ("create_", "update_", "delete_", "reverse_", "clear_"):
        assert verbe not in src, verbe


def test_le_hub_ne_lit_que_les_deux_instantanes():
    """Le budget de lecture du hub, épinglé (revue 2026-08-15 — la doctrine
    « no new Firestore query » était annoncée épinglée et ne l'était pas) :
    la route ne référence que les DEUX instantanés et les tables de libellés.
    Ajouter trust.list_transactions(...) ou al.list_reconciliations() ici
    serait une requête nouvelle par compte — la violation exacte."""
    path = os.path.join(_ATHENA, "routes", "comptabilite.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    allowed = {
        "trust.get_firm_trust_snapshot", "al.get_firm_admin_snapshot",
        "trust.ACCOUNT_TYPE_LABELS", "al.ACCOUNT_TYPE_LABELS",
    }
    used = set(re.findall(r"\b(?:trust|al)\.\w+", src))
    assert used <= allowed, used - allowed


# ── Le prédicat de conciliation en retard — miroir des deux côtés ─────────


def test_le_predicat_de_retard_lit_l_horloge_de_montreal():
    """2026-07-30T01:00Z = le 29 juillet, 21 h, à Montréal (EDT). En date
    UTC, (30 juil − 30 j) = 30 juin — une fin de mois — d'où due_through
    30 juin et un « Conciliation en retard » allumé dès 20 h LA VEILLE du
    bon jour ; en date de Montréal, (29 juil − 30 j) = 29 juin → due_through
    31 mai, et un compte concilié au 31 mai n'est PAS en retard. La bande du
    soir — la classe de bogue payée le 2026-08-02 et le 2026-08-14 (doctrine
    today_mtl). Horloge GELÉE, jamais dérivée du présent."""
    from datetime import datetime, timezone

    instant = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    last = datetime(2026, 5, 31, tzinfo=timezone.utc)
    for mod in (_rt.trust, _ra.al):
        assert mod._reconciliation_overdue(last, instant) is False, mod.__name__


def test_un_compte_ferme_n_est_jamais_en_retard(monkeypatch):
    """Aucune conciliation mensuelle n'est due après clôture : sans le garde,
    un compte fermé lisait « Conciliation en retard » POUR TOUJOURS (le
    prédicat ne connaît que des dates) — hub, tableau de bord et MCP à la
    fois. Les DEUX instantanés, et le drapeau cabinet avec eux."""
    ferme = {
        "id": "x1", "name": "Ancien compte", "status": "fermé",
        "book_balance": 0, "bank_balance": 0, "ledger_balance": 0,
        "account_type": "général", "created_at": None,
        "institution": "", "account_number_last4": "",
    }
    monkeypatch.setattr(_rt.trust, "list_accounts",
                        lambda status=None: [dict(ferme)])
    monkeypatch.setattr(_rt.trust, "list_reconciliations", lambda aid=None: [])
    monkeypatch.setattr(_rt.trust, "list_outstanding", lambda aid, as_of=None: [])
    monkeypatch.setattr(_rt.trust, "list_in_transit", lambda aid, as_of=None: [])
    snap = _rt.trust.get_firm_trust_snapshot()
    assert snap["accounts"][0]["reconciliation_overdue"] is False
    assert snap["reconciliation_overdue"] is False

    monkeypatch.setattr(_ra.al, "list_accounts",
                        lambda status=None: [dict(ferme, account_type="opérations")])
    monkeypatch.setattr(_ra.al, "list_reconciliations", lambda aid=None: [])
    snap = _ra.al.get_firm_admin_snapshot()
    assert snap["accounts"][0]["reconciliation_overdue"] is False
    assert snap["reconciliation_overdue"] is False
