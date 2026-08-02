"""Lot 5 — les prédicats du rapport de couverture, testés à nu.

``mcp/coverage.py`` n'importe rien de ``models`` : toute la suite de
contrôles s'éprouve sur des dictionnaires, sans Firestore et sans un seul
bouchon. C'est la raison d'être du module séparé.

Les trois amendements à la table du mandat sont épinglés ici, parce qu'ils
sont des décisions de fond et non des détails d'implantation :
« exempté » compte comme décidé, la signification se compte PAR PARTIE
ADVERSE, et CLIENT_INTROUVABLE existe pour qu'un contact supprimé ne se
lise pas comme un client vérifié.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The package __init__ reads config; coverage.py itself pulls in no model
# and constructs no Firestore client, which is the property that matters.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from mcp import coverage  # noqa: E402


def _ctx(*, active=(), protocols=None, clients=(), missing=()):
    return {
        "active_protocol_dossiers": set(active),
        "active_protocols_by_dossier": protocols or {},
        "clients_of": lambda v: list(clients),
        "missing_clients_of": lambda v: list(missing),
    }


def _d(**over):
    view = {
        "id": "d1", "file_number": "2026-001", "title": "T", "status": "actif",
        "forum_type": "judiciaire", "tribunal": "Cour supérieure",
        "court_file_number": "500-05-123456-241",
        "action": "REC-01", "action_a_valider": False,
        "valeur_cents": 1000000, "prescription_status": "courante",
        "opposing_parties": [], "significations": [], "client_ids": [],
    }
    view.update(over)
    return view


def _codes(view, ctx, **kw):
    return {f["code"] for f in coverage.run_checks(view, ctx, **kw)}


# ── Protocole ───────────────────────────────────────────────────────────


def test_protocole_absent_sur_une_instance_liee():
    assert "PROTO_ABSENT" in _codes(_d(), _ctx())
    assert "PROTO_ABSENT" not in _codes(_d(), _ctx(active=["d1"]))


def test_pas_de_protocole_attendu_sans_numero_de_cour():
    """Rien n'est déposé : NO_COUR_ABSENT le dit déjà, et exiger un
    protocole en plus serait un double signalement pour un seul fait."""
    codes = _codes(_d(court_file_number=""), _ctx())
    assert "PROTO_ABSENT" not in codes
    assert "NO_COUR_ABSENT" in codes


def test_forum_non_judiciaire_echappe_aux_obligations_du_cpc():
    """Un tribunal administratif ou une cour fédérale a ses propres règles."""
    for forum in ("administratif", "federal", "prejudiciaire"):
        codes = _codes(_d(forum_type=forum, court_file_number=""), _ctx())
        assert "PROTO_ABSENT" not in codes, forum
        assert "NO_COUR_ABSENT" not in codes, forum
        assert "SIGN_ABSENTE" not in codes, forum


def test_regime_inadequat_signale_le_type_et_le_tribunal():
    ctx = _ctx(active=["d1"],
               protocols={"d1": {"protocol_type": "cq_simplifié",
                                 "regime_mismatch": True}})
    findings = coverage.run_checks(_d(), ctx)
    proto = [f for f in findings if f["code"] == "PROTO_REGIME"]
    assert proto and "cq_simplifié" in proto[0]["detail"]
    assert "Cour supérieure" in proto[0]["detail"]


# ── Significations : par partie adverse ─────────────────────────────────


def test_signification_absente_quand_il_y_a_des_adverses():
    view = _d(opposing_parties=[{"id": "p1"}, {"id": "p2"}])
    assert "SIGN_ABSENTE" in _codes(view, _ctx())
    # Aucune partie adverse : rien à signifier.
    assert "SIGN_ABSENTE" not in _codes(_d(), _ctx())


def test_signification_partielle_compte_les_parties_adverses():
    view = _d(
        opposing_parties=[{"id": "p1"}, {"id": "p2"}],
        significations=[{"partie_id": "p1", "superseded_by": ""}],
    )
    codes = _codes(view, _ctx())
    assert "SIGN_PARTIELLE" in codes
    assert "SIGN_ABSENTE" not in codes      # le registre n'est pas vide


def test_toutes_signifiees_ne_signale_rien():
    view = _d(
        opposing_parties=[{"id": "p1"}, {"id": "p2"}],
        significations=[{"partie_id": "p1", "superseded_by": ""},
                        {"partie_id": "p2", "superseded_by": ""}],
    )
    codes = _codes(view, _ctx())
    assert "SIGN_PARTIELLE" not in codes and "SIGN_ABSENTE" not in codes


def test_une_signification_remplacee_ne_compte_pas():
    """Le cas du second procès-verbal corrigé : c'est celle que rien ne
    remplace qui est opérante."""
    view = _d(
        opposing_parties=[{"id": "p1"}],
        significations=[{"partie_id": "p1", "superseded_by": "sig-2"}],
    )
    assert coverage.operative_significations(view) == set()
    assert "SIGN_PARTIELLE" not in _codes(view, _ctx())   # 0 signifiée → absente
    assert "SIGN_ABSENTE" not in _codes(view, _ctx())     # registre non vide


def test_une_signification_au_propre_client_ne_masque_pas_un_defendeur():
    """La validation du modèle admet un id de CLIENT dans le registre (les
    délais courent par partie). Compter le registre brut laisserait une
    signification au client masquer un défendeur jamais signifié."""
    view = _d(
        opposing_parties=[{"id": "adv1"}],
        significations=[{"partie_id": "client1", "superseded_by": ""}],
    )
    assert coverage.operative_significations(view) == set()
    assert "SIGN_PARTIELLE" not in _codes(view, _ctx())


# ── Déontologie : les trois amendements ─────────────────────────────────


def test_exempte_compte_comme_decide():
    """La règle littérale du mandat (≠ vérifié) produirait un FAUX
    manquement sur un client légitimement exempté — sur un contrôle
    réglementaire, là où un faux positif coûte le plus cher."""
    exempte = {"identity_verified": "exempté", "conflict_check": "vérifié"}
    assert "IDENTITE_NON_VERIFIEE" not in _codes(_d(), _ctx(clients=[exempte]))
    non_verifie = {"identity_verified": "non_vérifié", "conflict_check": "vérifié"}
    assert "IDENTITE_NON_VERIFIEE" in _codes(_d(), _ctx(clients=[non_verifie]))


def test_conflit_detecte_compte_comme_decide():
    """Le contrôle A ÉTÉ fait ; son résultat est un conflit. C'est une
    information, pas un manquement de saisie."""
    detecte = {"identity_verified": "vérifié", "conflict_check": "conflit_détecté"}
    assert "CONFLIT_NON_VERIFIE" not in _codes(_d(), _ctx(clients=[detecte]))


def test_client_introuvable_est_un_manquement_a_part():
    """Sans lui, un contact supprimé sort simplement du balayage : les deux
    contrôles déontologiques rendraient « propre » un dossier qui ne cite
    plus aucun client existant."""
    codes = _codes(_d(client_ids=["p9"]), _ctx(clients=[], missing=["p9"]))
    assert "CLIENT_INTROUVABLE" in codes
    assert "CONFLIT_NON_VERIFIE" not in codes      # aucun client lu
    assert "IDENTITE_NON_VERIFIEE" not in codes


# ── Signalements ────────────────────────────────────────────────────────


def test_valeur_zero_est_une_valeur():
    """`is None`, jamais un test de fausseté : 0 est une valeur (recours
    purement déclaratoire), et la traiter comme absente harcèlerait sur un
    champ décidé."""
    assert "VALEUR_ABSENTE" not in _codes(_d(valeur_cents=0), _ctx())
    assert "VALEUR_ABSENTE" in _codes(_d(valeur_cents=None), _ctx())


def test_prejudiciaire_est_un_numero_manquant_pas_un_numero():
    """« Préjudiciaire » est le substitut qu'un dossier pré-litige porte
    pour que les gabarits citent quelque chose ; sur un forum judiciaire il
    signifie que le vrai numéro n'a jamais été saisi."""
    assert "NO_COUR_ABSENT" in _codes(_d(court_file_number="Préjudiciaire"), _ctx())
    assert "NO_COUR_ABSENT" not in _codes(_d(), _ctx())


def test_prescription_a_verifier_et_taxonomie():
    assert "PRESCRIPTION_A_VERIFIER" in _codes(
        _d(prescription_status="a_verifier"), _ctx())
    for ok in ("courante", "interrompue", "echue", "imprescriptible"):
        assert "PRESCRIPTION_A_VERIFIER" not in _codes(
            _d(prescription_status=ok), _ctx()), ok
    assert "TAXONOMIE_A_VALIDER" in _codes(_d(action_a_valider=True), _ctx())


# ── Invariants du registre ──────────────────────────────────────────────


def test_skip_retire_vraiment_le_controle():
    view = _d(valeur_cents=None)
    assert "VALEUR_ABSENTE" in _codes(view, _ctx())
    assert "VALEUR_ABSENTE" not in _codes(
        view, _ctx(), skip=frozenset({"VALEUR_ABSENTE"}))


def test_un_dossier_sain_ne_produit_rien():
    view = _d(client_ids=["p1"])
    clean = {"identity_verified": "vérifié", "conflict_check": "vérifié"}
    assert coverage.run_checks(view, _ctx(active=["d1"], clients=[clean])) == []


def test_chaque_detail_renvoie_a_l_application_jamais_au_connecteur():
    """Le rapport crée un appel à l'action auquel le connecteur ne doit pas
    répondre : il ne peut ni créer un protocole, ni vérifier une identité,
    ni signifier. Un detail qui le laisserait croire inviterait une écriture
    interdite."""
    view = _d(court_file_number="", valeur_cents=None, action_a_valider=True,
              prescription_status="a_verifier",
              opposing_parties=[{"id": "p1"}], client_ids=["p9"])
    ctx = _ctx(clients=[{"identity_verified": "non_vérifié",
                         "conflict_check": "non_vérifié"}], missing=["p9"])
    findings = coverage.run_checks(view, ctx)
    assert findings
    for f in findings:
        assert f["detail"], f["code"]
        # Aucun detail ne doit promettre une action du connecteur.
        lowered = f["detail"].lower()
        for forbidden in ("je vais", "je peux", "créons", "j'ai créé"):
            assert forbidden not in lowered, (f["code"], f["detail"])


def test_le_registre_est_coherent():
    codes = [c[0] for c in coverage.CHECKS]
    assert len(codes) == len(set(codes))                  # pas de doublon
    assert set(coverage.ALL_CODES) == set(codes) | set(coverage.CROSS_SCOPE_CODES)
    for code in coverage.ALL_CODES:
        assert code in coverage.SEVERITY_BY_CODE, code
        assert code in coverage.LABEL_BY_CODE, code
        assert coverage.SEVERITY_BY_CODE[code] in (
            coverage.MANQUEMENT, coverage.SIGNALEMENT), code


def test_les_deux_controles_deontologiques_sont_des_manquements():
    """Ce sont des obligations réglementaires, pas des préférences de
    saisie — la distinction que l'avocat a demandé de préserver."""
    for code in ("CONFLIT_NON_VERIFIE", "IDENTITE_NON_VERIFIEE"):
        assert coverage.SEVERITY_BY_CODE[code] == coverage.MANQUEMENT
