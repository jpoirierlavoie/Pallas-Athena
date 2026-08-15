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

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)


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
