"""Les outils de reprise historique du connecteur (lot Q).

Même amorce que test_mcp_tools : on importe handlers/tools SOUS le correctif de
google.cloud.firestore.Client (models/__init__ construit son client à
l'import), puis on remplace les verbes de modèle sur les références liées du
module handlers.
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import mcp.tools as tools


# ── get_reference_vocabulary ───────────────────────────────────────────────
# Le déblocage : « Domaine invalide. » ne nomme aucun domaine valide, et aucun
# outil de lecture n'exposait la taxonomie. La classification que le juriste
# demande d'importer ne pouvait qu'être devinée, puis refusée.


@pytest.mark.parametrize(
    "kind",
    ["domaines", "actions", "prescription_types", "forums", "districts", "phases"],
)
def test_chaque_vocabulaire_rend_des_codes_utilisables(kind):
    payload = handlers.get_reference_vocabulary({"kind": kind})
    assert payload["count"] > 0
    assert payload["kind"] == kind
    for item in payload["items"]:
        assert item["code"]
        assert item["label"]
        assert isinstance(item["note"], str)


def test_les_domaines_sont_ceux_que_le_modele_valide():
    """Dérivé de utils.taxonomie, jamais recopié : le module est PUR, donc la
    dérive est structurellement impossible (le précédent _COVERAGE_CODES)."""
    from utils import taxonomie

    codes = [i["code"] for i in
             handlers.get_reference_vocabulary({"kind": "domaines"})["items"]]
    assert codes == [c for c in taxonomie.VALID_DOMAINES if c]


def test_les_actions_se_filtrent_par_domaine_et_en_portent_le_prefixe():
    payload = handlers.get_reference_vocabulary(
        {"kind": "actions", "domaine": "REC"}
    )
    assert payload["count"] > 0
    for item in payload["items"]:
        assert item["code"].startswith("REC-")


def test_sans_filtre_les_actions_sortent_toutes():
    from utils import taxonomie

    payload = handlers.get_reference_vocabulary({"kind": "actions"})
    assert payload["count"] == len(taxonomie.ACTIONS)


def test_un_domaine_inconnu_est_refuse_en_nommant_la_sortie():
    with pytest.raises(tools.ToolArgumentError) as exc:
        handlers.get_reference_vocabulary(
            {"kind": "actions", "domaine": "ZZZ"}
        )
    assert "domaines" in str(exc.value)


def test_le_filtre_domaine_ne_s_applique_qu_aux_actions():
    """Un filtre silencieusement ignoré ferait croire à une liste restreinte
    alors qu'elle est complète."""
    with pytest.raises(tools.ToolArgumentError):
        handlers.get_reference_vocabulary(
            {"kind": "domaines", "domaine": "REC"}
        )


def test_le_vocabulaire_des_phases_porte_les_codes_ET_les_sous_codes():
    items = handlers.get_reference_vocabulary({"kind": "phases"})["items"]
    codes = {i["code"] for i in items}
    assert "CTS" in codes and "CTS-02" in codes
    parent = next(i for i in items if i["code"] == "CTS")
    child = next(i for i in items if i["code"] == "CTS-02")
    assert parent["note"] == "phase"
    assert "CTS" in child["note"]


def test_le_delai_d_une_action_est_annonce_comme_indicatif():
    """La taxonomie SUGGÈRE un délai, elle n'en fixe jamais un. Le « » de
    certaines lignes est voulu (la source n'a pas de période unique) : il ne
    doit pas se lire comme une lacune à combler."""
    description = tools.TOOLS["get_reference_vocabulary"]["description"]
    assert "INDICATIVE" in description
    payload = handlers.get_reference_vocabulary(
        {"kind": "actions", "domaine": "REC"}
    )
    assert any(i["note"] for i in payload["items"])


# ── find_imported ──────────────────────────────────────────────────────────


@pytest.fixture
def legacy(monkeypatch):
    """Remplace models.find_by_legacy_ref — le gestionnaire l'importe
    LOCALEMENT, donc l'attribut est relu à l'appel."""
    import models

    store: dict[str, list[dict]] = {}

    def _find(collection, legacy_ref, limit=5):
        return list(store.get(collection, []))

    monkeypatch.setattr(models, "find_by_legacy_ref", _find)
    return store


def test_find_imported_retrouve_a_travers_les_collections(legacy):
    legacy["dossiers"] = [{"id": "d1", "file_number": "2019-014",
                           "title": "Tremblay c. Lavoie"}]
    legacy["invoices"] = [{"id": "i1", "invoice_number": "2019-F014",
                           "dossier_id": "d1"}]
    payload = handlers.find_imported({"legacy_ref": "L-42"})
    kinds = {m["entity_type"] for m in payload["matches"]}
    assert kinds == {"dossier", "invoice"}
    assert payload["count"] == 2
    assert payload["legacy_ref"] == "L-42"
    facture = next(m for m in payload["matches"] if m["entity_type"] == "invoice")
    assert facture["label"] == "2019-F014"
    assert facture["dossier_id"] == "d1"


def test_find_imported_se_restreint_a_un_type(legacy):
    legacy["dossiers"] = [{"id": "d1", "file_number": "2019-014", "title": "X"}]
    legacy["invoices"] = [{"id": "i1", "invoice_number": "2019-F014"}]
    payload = handlers.find_imported(
        {"legacy_ref": "L-42", "entity_type": "dossier"}
    )
    assert [m["entity_type"] for m in payload["matches"]] == ["dossier"]


def test_find_imported_sans_correspondance_rend_zero(legacy):
    payload = handlers.find_imported({"legacy_ref": "L-inconnue"})
    assert payload["count"] == 0
    assert payload["matches"] == []


def test_find_imported_echoue_ferme(monkeypatch):
    """« Rien n'est revenu, donc je crée » est le geste suivant. Une erreur
    avalée se lirait « absent » et frapperait un doublon que RIEN dans ce
    connecteur ne peut supprimer."""
    import models

    def _boom(collection, legacy_ref, limit=5):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(models, "find_by_legacy_ref", _boom)
    with pytest.raises(RuntimeError):
        handlers.find_imported({"legacy_ref": "L-42"})


def test_find_imported_refuse_une_reference_vide(legacy):
    for blank in ("", "   "):
        with pytest.raises(tools.ToolArgumentError):
            handlers.find_imported({"legacy_ref": blank})


def test_find_imported_nomme_un_contact_par_son_nom_affiche(legacy):
    legacy["parties"] = [{"id": "p1", "type": "organization",
                          "organization_name": "Béton Nord inc."}]
    payload = handlers.find_imported({"legacy_ref": "L-7"})
    assert payload["matches"][0]["label"] == "Béton Nord inc."
    assert payload["matches"][0]["dossier_id"] is None


def test_les_deux_nouveaux_outils_restent_en_lecture_seule():
    """Ils informent la reprise ; ils n'écrivent rien. Un scope d'écriture
    déclaré ici les retirerait d'un jeton en lecture seule sans raison."""
    for name in ("get_reference_vocabulary", "find_imported"):
        assert name not in tools.WRITE_TOOLS
        assert tools.required_scope(name) == "athena:read"
        assert "scope" not in tools.TOOLS[name]
