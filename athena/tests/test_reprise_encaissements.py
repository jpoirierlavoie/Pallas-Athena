"""La DÉCISION de la reprise des encaissements d'honoraires.

Aucun test ne peut protéger une exécution réelle : chaque écriture que ce
script produit porte `invoice_id` ET `trust_transaction_id`, donc elle refuse
pour toujours la modification, la suppression et la contre-passation depuis
Administration. Les tests visent donc la décision, et l'exécuteur est tenu
trop bête pour en contenir une — le marché que `test_purge_encaissements`
énonçait déjà pour son aîné.

Zéro écriture ici : tout ce qui écrit est bouchonné ou piégé.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from scripts import reprise_encaissements as rep

UTC = timezone.utc


def _d(y, m, j):
    return datetime(y, m, j, tzinfo=UTC)


def _facture(fid="fac1", **over):
    doc = {"id": fid, "invoice_number": "2026-F001", "status": "envoyée",
           "amount_due": 100000, "amount_paid": 0, "dossier_id": "dos1",
           "legacy_ref": ""}
    doc.update(over)
    return doc


def _virement(tid="ttx1", **over):
    doc = {"id": tid, "purpose": "virement_honoraires", "amount": 50000,
           "date": _d(2026, 3, 1), "sequence": 10, "status": "compensée",
           "dossier_id": "dos1", "dossier_file_number": "2026-001",
           "invoice_id": None, "invoice_external_ref": "", "client_name": "Client",
           "reference": "", "reversed_by_id": None}
    doc.update(over)
    return doc


# ═══════════════════════════════════════════════════════════════════════════
# Le rapprochement — la seule place où le script devine
# ═══════════════════════════════════════════════════════════════════════════


def test_la_normalisation_efface_les_tirets_pas_le_reste():
    """Sans elle, sept virements ne se rapprochent d'aucune facture qui
    existe pourtant : la reprise a perdu les tirets en chemin."""
    assert rep.normaliser("251601-01") == rep.normaliser("25160101")
    assert rep.normaliser("2026-F007") == "2026F007"
    assert rep.normaliser("") == ""
    # Elle ne confond pas deux numéros distincts.
    assert rep.normaliser("2565") != rep.normaliser("256501-01")


def test_une_facture_indexee_deux_fois_ne_devient_pas_ambigue():
    """Le numéro et le legacy_ref se normalisent souvent pareil ; compter deux
    fois la même facture la ferait passer pour ambiguë et la rendrait
    inutilisable."""
    f = _facture(invoice_number="250701-01", legacy_ref="facture:25070101")
    index = rep.indexer_factures([f])
    assert index["25070101"] == [f]


def test_resolution_par_identifiant_ignore_l_index():
    f = _facture()
    trouvee, motif = rep.resoudre(
        _virement(invoice_id="fac1"), {}, {"fac1": f})
    assert trouvee is f and motif == ""


def test_la_correspondance_est_exacte_jamais_un_prefixe():
    f = _facture(invoice_number="256501-01")
    index = rep.indexer_factures([f])
    _, motif = rep.resoudre(
        _virement(invoice_external_ref="2565"), index, {})
    assert "pas encore repris" in motif


def test_un_numero_porte_par_deux_factures_est_refuse():
    a = _facture("fac1", invoice_number="250701-01")
    b = _facture("fac2", invoice_number="2507-0101")
    index = rep.indexer_factures([a, b])
    _, motif = rep.resoudre(
        _virement(invoice_external_ref="250701-01"), index, {})
    assert "ambigu" in motif


def test_un_numero_reattribue_par_l_ancien_systeme_se_trahit_au_dossier():
    """Le filet qui a servi dès le premier essai : « 250701-01 » se rapproche
    pour un virement et pas pour deux autres, parce que ceux-là viennent d'un
    autre dossier."""
    f = _facture(invoice_number="250701-01", dossier_id="dosA")
    index = rep.indexer_factures([f])
    _, motif = rep.resoudre(
        _virement(invoice_external_ref="250701-01", dossier_id="dosB"),
        index, {})
    assert "autre dossier" in motif


def test_un_virement_sans_reference_ne_devine_rien():
    _, motif = rep.resoudre(_virement(), {}, {})
    assert motif == "aucune facture citée"


# ═══════════════════════════════════════════════════════════════════════════
# L'invariant qui exclut le piège de saturation
# ═══════════════════════════════════════════════════════════════════════════


def test_la_somme_du_groupe_se_compare_au_du_fige_jamais_au_solde_vivant():
    """`amount_paid` bouge d'une exécution à l'autre, `amount_due` non : c'est
    ce qui rend l'invariant stable au rejeu."""
    f = _facture(amount_due=100000, amount_paid=90000)
    assert rep.motif_inexecutable(f, 100000, 0) == ""      # tient sur le dû
    assert rep.motif_inexecutable(f, 100001, 0) != ""


def test_ce_que_le_registre_porte_deja_entre_dans_le_compte():
    f = _facture(amount_due=100000)
    assert rep.motif_inexecutable(f, 60000, 40000) == ""
    assert "totalisent" in rep.motif_inexecutable(f, 60000, 40001)


@pytest.mark.parametrize("statut,attendu", [
    ("annulée", "annulée"),
    ("brouillon", "brouillon"),
])
def test_une_facture_non_emise_ne_recoit_pas_d_encaissement(statut, attendu):
    """Le garde qui rend mécanique la décision d'attendre la fin de la
    reprise : tant qu'une facture est au brouillon, rien ne s'y impute."""
    assert attendu in rep.motif_inexecutable(_facture(status=statut), 1, 0)


def test_une_facture_sans_du_est_refusee():
    assert "sans solde" in rep.motif_inexecutable(_facture(amount_due=0), 1, 0)


# ═══════════════════════════════════════════════════════════════════════════
# Le va-et-vient CSV
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("valeur", [
    "-Constructions Nord", "=SOMME(A1)", "+1", "@client", "Tremblay", "",
])
def test_le_va_et_vient_rend_la_valeur_proposee(valeur):
    """La neutralisation protège le tableur ; sa réciproque protège la
    donnée. Sans elle, un nom de client commençant par un tiret reviendrait
    avec une apostrophe collée devant."""
    assert rep.deneutraliser(rep.neutraliser(valeur)) == valeur


# ═══════════════════════════════════════════════════════════════════════════
# Les lectures
# ═══════════════════════════════════════════════════════════════════════════


class _Snap:
    def __init__(self, data, doc_id="x"):
        self._d = data
        self.id = doc_id

    def to_dict(self):
        return dict(self._d)


class _Col:
    def __init__(self, rows):
        self._rows = rows

    def stream(self):
        return iter(self._rows)


@pytest.fixture()
def base(monkeypatch):
    box = {"trust": [], "invoices": []}

    class _Db:
        def collection(self, name):
            if name == "invoices":
                return _Col([_Snap(f, f["id"]) for f in box["invoices"]])
            return _Col([_Snap(t, t["id"]) for t in box["trust"]])

    monkeypatch.setattr(rep, "db", _Db())
    return box


def test_un_virement_contrepasse_est_ecarte_mais_nomme(base):
    """Il n'a rien déplacé net : l'inscrire enregistrerait une recette revenue
    au client. Mais un décompte qui ne retombe pas sur 40 doit s'expliquer."""
    base["trust"] = [
        _virement("ok"),
        _virement("contrepasse", reversed_by_id="rev1"),
        _virement("annule", status="annulée"),
    ]
    retenus, exclus = rep.lire_virements()
    assert [t["id"] for t in retenus] == ["ok"]
    assert len(exclus) == 2


def test_les_virements_sortent_dans_l_ordre_chronologique(base):
    """L'ordre n'est pas cosmétique : il décide de la date que portera la
    facture (`paid_date` est écrasé à chaque imputation) et il conditionne
    l'invariant qui empêche une facture de saturer avant son dernier
    virement."""
    base["trust"] = [
        _virement("c", date=_d(2026, 5, 1)),
        _virement("a", date=_d(2026, 1, 1)),
        _virement("b", date=_d(2026, 3, 1)),
    ]
    retenus, _ = rep.lire_virements()
    assert [t["id"] for t in retenus] == ["a", "b", "c"]


def test_seulement_restreint_a_un_virement(base):
    """La première exécution réelle passe par là : elle convertit une classe
    d'erreur irréparable en une seule ligne irréparable."""
    base["trust"] = [_virement("a"), _virement("b")]
    retenus, _ = rep.lire_virements(seulement="b")
    assert [t["id"] for t in retenus] == ["b"]


# ═══════════════════════════════════════════════════════════════════════════
# L'idempotence
# ═══════════════════════════════════════════════════════════════════════════


def test_l_etat_lit_toutes_les_ecritures_et_voit_le_doublon(monkeypatch):
    monkeypatch.setattr(rep.al, "list_by_trust_transaction",
                        lambda t: [{"id": "a"}, {"id": "b"}])
    etat, _, motif = rep._etat("ttx1")
    assert etat == "refus" and "2 écritures" in motif


@pytest.mark.parametrize("ecritures,attendu", [
    ([], "à_créer"),
    ([{"id": "a", "status": "en_circulation"}], "à_compenser"),
    ([{"id": "a", "status": "compensée"}], "faite"),
])
def test_l_etat_permet_de_reprendre_ou_l_on_s_est_arrete(
    monkeypatch, ecritures, attendu
):
    """La composition a trois temps et peut s'arrêter après chacun : rejouer
    tout serait doubler."""
    monkeypatch.setattr(rep.al, "list_by_trust_transaction", lambda t: ecritures)
    assert rep._etat("ttx1")[0] == attendu


def test_une_lecture_ratee_arrete_la_reprise_au_lieu_de_doubler(monkeypatch):
    """LA raison d'être de `list_by_trust_transaction`. Son voisin échoue
    OUVERT : il ferait conclure « rien n'est écrit » et doublerait une
    écriture qu'on ne peut plus corriger."""
    def _boum(_t):
        raise RuntimeError("firestore indisponible")
    monkeypatch.setattr(rep.al, "list_by_trust_transaction", _boum)
    with pytest.raises(RuntimeError):
        rep._etat("ttx1")


# ═══════════════════════════════════════════════════════════════════════════
# L'exécution
# ═══════════════════════════════════════════════════════════════════════════


def test_une_projection_ratee_arrete_TOUT(monkeypatch, base):
    """L'écriture est COMMISE quand la projection échoue, et elle porte
    invoice_id + trust_transaction_id : elle est incorrigible depuis
    l'application. Continuer la boucle multiplierait ce cas."""
    faites = []
    monkeypatch.setattr(rep.al, "create_transaction",
                        lambda d: (faites.append(d) or {
                            "id": f"adm{len(faites)}", "status": "compensée",
                            "invoice_id": d["invoice_id"], "date": d["date"]}, []))
    monkeypatch.setattr(rep.al, "clear_transaction", lambda i, d: (None, []))
    monkeypatch.setattr(rep, "get_invoice", lambda i: _facture(status="envoyée"))
    monkeypatch.setattr("routes.admin_ledger._projeter_paiement", lambda e: False)

    actions = [
        {"virement": _virement("t1"), "facture": _facture(), "mode": "encaissement",
         "etat": "à_créer", "ecriture": None},
        {"virement": _virement("t2"), "facture": _facture(), "mode": "encaissement",
         "etat": "à_créer", "ecriture": None},
    ]
    echecs = rep.appliquer("cpt1", actions)
    assert echecs and "n'a PAS été créditée" in echecs[0]
    assert len(faites) == 1, "la boucle a continué après une projection ratée"


def test_l_execution_n_ecrit_JAMAIS_au_fideicommis(monkeypatch, base):
    """Le chemin automatisé du logiciel n'écrit jamais vers le fidéicommis ;
    la reprise non plus. Le lien vit sur l'écriture d'administration."""
    monkeypatch.setattr(rep.al, "create_transaction",
                        lambda d: ({"id": "adm1", "status": "compensée",
                                    "invoice_id": d["invoice_id"],
                                    "date": d["date"]}, []))
    monkeypatch.setattr(rep.al, "clear_transaction", lambda i, d: (None, []))
    monkeypatch.setattr(rep, "get_invoice", lambda i: _facture(status="envoyée"))
    monkeypatch.setattr("routes.admin_ledger._projeter_paiement", lambda e: True)

    class _Piege:
        def collection(self, name):
            if "trust" in name:
                pytest.fail("la reprise a touché le fidéicommis")
            return _Col([])
    monkeypatch.setattr(rep.trust, "db", _Piege(), raising=False)

    actions = [{"virement": _virement(), "facture": _facture(),
                "mode": "encaissement", "etat": "à_créer", "ecriture": None}]
    assert rep.appliquer("cpt1", actions) == []


def test_l_ecriture_porte_le_virement_et_la_facture(monkeypatch, base):
    """Le lien machine choisi par le juriste, et la provenance en clair — sans
    quoi rien ne distinguerait une reprise d'une saisie contemporaine."""
    vues = {}
    monkeypatch.setattr(rep.al, "create_transaction",
                        lambda d: (vues.update(d) or {
                            "id": "adm1", "status": "compensée",
                            "invoice_id": d["invoice_id"], "date": d["date"]}, []))
    monkeypatch.setattr(rep.al, "clear_transaction", lambda i, d: (None, []))
    monkeypatch.setattr(rep, "get_invoice", lambda i: _facture(status="envoyée"))
    monkeypatch.setattr("routes.admin_ledger._projeter_paiement", lambda e: True)

    rep.appliquer("cpt1", [{
        "virement": _virement(invoice_external_ref="WP1820000001-01"),
        "facture": _facture(), "mode": "encaissement",
        "etat": "à_créer", "ecriture": None}])

    assert vues["trust_transaction_id"] == "ttx1"
    assert vues["invoice_id"] == "fac1"
    assert vues["kind"] == "encaissement_facture"
    assert vues["direction"] == "recette"
    assert "reprise historique" in vues["description"]
    assert "WP1820000001-01" in vues["description"]


def test_une_recette_autre_ne_porte_aucune_facture(monkeypatch, base):
    """Un virement qui ne peut pas s'imputer entier se porte SANS lien de
    facture — jamais en deux morceaux, ce qui défairait la clé
    d'idempotence."""
    vues = {}
    monkeypatch.setattr(rep.al, "create_transaction",
                        lambda d: (vues.update(d) or {
                            "id": "adm1", "status": "compensée",
                            "invoice_id": None, "date": d["date"]}, []))
    monkeypatch.setattr(rep.al, "clear_transaction", lambda i, d: (None, []))
    monkeypatch.setattr("routes.admin_ledger._projeter_paiement",
                        lambda e: pytest.fail("aucune projection attendue"))

    rep.appliquer("cpt1", [{"virement": _virement(), "facture": None,
                            "mode": "recette_autre", "etat": "à_créer",
                            "ecriture": None}])
    assert vues["kind"] == "recette_autre"
    assert vues["invoice_id"] is None
    assert vues["trust_transaction_id"] == "ttx1"


# ═══════════════════════════════════════════════════════════════════════════
# La coquille
# ═══════════════════════════════════════════════════════════════════════════


def test_le_compte_doit_etre_un_compte_d_operations_actif(monkeypatch, base):
    monkeypatch.setattr(rep.al, "get_account",
                        lambda i: {"account_type": "carte_crédit", "status": "actif"})
    _, refus = rep.planifier("carte", [], None)
    assert refus and "carte de crédit" in refus[0]

    monkeypatch.setattr(rep.al, "get_account",
                        lambda i: {"account_type": "opérations", "status": "fermé"})
    _, refus = rep.planifier("cpt1", [], None)
    assert refus and "fermé" in refus[0]


def test_un_mode_inconnu_est_refuse(monkeypatch, base):
    monkeypatch.setattr(rep.al, "get_account",
                        lambda i: {"account_type": "opérations", "status": "actif"})
    monkeypatch.setattr(rep.al, "list_by_trust_transaction", lambda t: [])
    base["trust"] = [_virement("t1")]
    _, refus = rep.planifier(
        "cpt1", [{"trust_tx_id": "t1", "mode": "payer"}], None)
    assert refus and "mode « payer » inconnu" in refus[0]


def test_ignorer_n_inscrit_rien(monkeypatch, base):
    monkeypatch.setattr(rep.al, "get_account",
                        lambda i: {"account_type": "opérations", "status": "actif"})
    base["trust"] = [_virement("t1")]
    actions, refus = rep.planifier(
        "cpt1", [{"trust_tx_id": "t1", "mode": "ignorer"}], None)
    assert actions == [] and refus == []
