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
        assert f'id="{prefix}-export"' in src, rep
        assert f'id="{prefix}-header"' in src, rep
        assert 'hx-swap-oob="true"' in src, rep
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
