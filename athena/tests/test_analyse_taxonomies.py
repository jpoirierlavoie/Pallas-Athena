"""Analyse documentaire — les tables (annexes A/B/D) et les dérivations.

TOUT ce fichier est PUR : aucun modèle, aucun Firestore, aucun appel
réseau. C'est le point : avec le partage « le modèle observe, le code
qualifie », la matrice de protection du §14 en entier est une suite
d'assertions sur des fonctions pures — elle se prouve sans quota.
"""

import os
import re
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from utils import analyse_protection as prot  # noqa: E402
from utils import analyse_taxonomies as tax  # noqa: E402


# ── Les tables ──────────────────────────────────────────────────────────


def test_toute_nature_existe_dans_le_modele():
    """La table ne peut pas inventer une catégorie de document.

    Importé sous le mock Firestore (le motif test_document_vocab) : le
    module de taxonomie, lui, reste Firestore-free.
    """
    with mock.patch("google.cloud.firestore.Client"):
        import models.document as doc

    for code, entry in tax.SOUS_NATURES.items():
        assert entry.nature in doc.VALID_CATEGORIES, f"{code} → {entry.nature}"


def test_la_valeur_heritee_n_est_jamais_produite():
    # « procès_verbal » reste lisible et filtrable dans l'application, mais
    # aucune analyse ne doit le produire : c'est la scission qui existe
    # pour ça.
    assert "procès_verbal" not in tax.VALID_NATURES
    assert "procès_verbal_signification" in tax.VALID_NATURES
    assert "procès_verbal_audience" in tax.VALID_NATURES


def test_les_codes_frappes_sont_ascii_majuscules():
    motif = re.compile(r"^[A-Z][A-Z0-9_]*$")
    for code in tax.VALID_SOUS_NATURES:
        assert motif.match(code), code
    for code in tax.VALID_PRIVILEGES:
        assert motif.match(code), code
    for famille in tax.VALID_FAMILLES:
        assert motif.match(famille), famille


def test_les_libelles_sont_du_francais():
    for entry in tax.SOUS_NATURES.values():
        assert entry.libelle and entry.libelle[0].isupper(), entry.code


def test_chaque_sous_nature_a_exactement_une_famille():
    for code, entry in tax.SOUS_NATURES.items():
        assert entry.famille in tax.VALID_FAMILLES, code
        assert tax.famille_of(code) == entry.famille


def test_les_champs_attendus_et_possibles_sont_disjoints():
    for code, entry in tax.SOUS_NATURES.items():
        assert not set(entry.champs) & set(entry.champs_possibles), code


def test_les_exemptions_portent_sur_des_champs_attendus():
    for code, entry in tax.SOUS_NATURES.items():
        assert set(entry.champs_sans_penalite) <= set(entry.champs), code


def test_les_defauts_de_protection_sont_dans_la_taxonomie():
    for code, entry in tax.SOUS_NATURES.items():
        for p in entry.protection_defaut:
            assert p in tax.PRIVILEGES, f"{code} → {p}"


def test_les_implications_pointent_vers_des_codes_vivants():
    for code, p in tax.PRIVILEGES.items():
        for cible in p.implique:
            assert cible in tax.PRIVILEGES, f"{code} → {cible}"


def test_le_secret_professionnel_est_seul_au_niveau_trois():
    niveau_3 = [c for c, p in tax.PRIVILEGES.items() if p.niveau == 3]
    assert niveau_3 == ["SECRET_PROFESSIONNEL"]


def test_le_secret_commercial_est_au_niveau_un_avec_sa_reserve():
    # Arbitrage 2026-08-26, contre l'annexe D : les art. 1472/1612 C.c.Q.
    # ne fondent aucune immunité de production. Le niveau 2 ferait dire à
    # l'échelle une chose fausse — et le consommateur MCP ne voit que
    # l'entier.
    p = tax.PRIVILEGES["SECRET_COMMERCIAL"]
    assert p.niveau == 1
    assert "immunité de production" in p.reserve


def test_les_codes_retires_de_la_v1_sont_absents():
    for code in ("INFORMATEUR", "INTERET_COMMUN"):
        assert code not in tax.PRIVILEGES


def test_none_est_une_cle_explicite_des_libelles_de_niveau():
    # Un rendu qui ferait `.get(x, "Public")` inverserait exactement
    # l'asymétrie que tout le module protège.
    assert tax.NIVEAU_LABELS[None] == "Non déterminé"
    assert tax.NIVEAU_LABELS[0] == "Public"


def test_les_juridictions_familiales_sont_epinglees_sur_reference():
    """Recopiées ici (le module doit rester Firestore-free), re-dérivées ici."""
    with mock.patch("google.cloud.firestore.Client"):
        from models import reference

    attendu = {
        code
        for code, j in reference._JURIDICTIONS.items()
        if "familiale" in j["competence"].lower()
    }
    assert tax.JURIDICTIONS_ACCES_RESTREINT == attendu


def test_les_enums_du_schema_derivent_de_la_table():
    enums = tax.schema_enums()
    assert enums["sous_nature"] == [s.code for s in tax._SOUS_NATURES]
    assert set(enums["privileges"]) == set(tax.VALID_PRIVILEGES)
    assert set(enums["nature"]) == set(tax.VALID_NATURES)
    # Stable entre deux constructions : le schéma compilé et le préfixe de
    # cache de prompt en dépendent.
    assert tax.schema_enums() == enums


# ── Accès strict, jamais par préfixe ────────────────────────────────────


def test_famille_par_acces_strict_jamais_par_prefixe():
    # PV_AUDIENCE est un préfixe strict de PV_AUDIENCE_JUGEMENT, et
    # PV_SIGNIFICATION de PV_SIGNIFICATION_DESIGNEE : le piège
    # « conférence » de hearing_type, à l'intérieur de l'annexe A.
    assert tax.famille_of("PV_AUDIENCE") == tax.JUDICIAIRE
    assert tax.famille_of("PV_AUDIENCE_JUGEMENT") == tax.JUDICIAIRE
    assert tax.famille_of("PV_SIGNIFICATION_DESIGNEE") == tax.JUDICIAIRE
    # Un code voisin mais inconnu ne se résout PAS par préfixe.
    assert tax.famille_of("PV_AUDIENCE_X") == tax.INDETERMINE
    assert tax.famille_of("") == tax.INDETERMINE


def test_aucune_resolution_par_prefixe_dans_les_sources():
    from pathlib import Path

    racine = Path(__file__).resolve().parent.parent / "utils"
    for nom in ("analyse_taxonomies.py", "analyse_protection.py"):
        src = (racine / nom).read_text(encoding="utf-8")
        # Les mentions en commentaire sont permises ; un APPEL ne l'est pas.
        assert ".startswith(" not in src, nom


def test_validate_pair_refuse_un_couple_incoherent():
    assert prot is not None  # le module importe
    assert tax.validate_pair("procédure", "PROC_DEFENSE") == []
    erreurs = tax.validate_pair("preuve", "PROC_DEFENSE")
    assert erreurs and "procédure" in erreurs[0]
    erreurs = tax.validate_pair("preuve", "CODE_INVENTE")
    assert erreurs and "inconnue" in erreurs[0]


# ── §5.4 — la bascule du procès-verbal portant jugement ─────────────────


def test_la_matrice_bascule_sur_le_jugement_quand_dispositif():
    attendus, _, _ = tax.matrice_champs("PV_AUDIENCE", contient_dispositif=True)
    assert tax.CHAMP_DISPOSITIF in attendus
    attendus, _, _ = tax.matrice_champs("PV_AUDIENCE", contient_dispositif=False)
    assert tax.CHAMP_DISPOSITIF not in attendus


def test_un_dispositif_inconnu_bascule_aussi():
    # L'asymétrie : ce qu'on tairait en traitant l'inconnu comme « pas de
    # jugement », c'est un délai d'appel.
    attendus, _, _ = tax.matrice_champs("PV_AUDIENCE", contient_dispositif=None)
    assert tax.CHAMP_DISPOSITIF in attendus
    assert prot.alerte_dispositif_detecte("PV_AUDIENCE", None) is True
    assert prot.alerte_dispositif_detecte("PV_AUDIENCE", False) is False
    assert prot.alerte_dispositif_detecte("PV_AUDIENCE_JUGEMENT", False) is True
    assert prot.alerte_dispositif_detecte("JUG_JUGEMENT", None) is False


def test_l_arret_n_ancre_que_le_dispositif_et_les_juges():
    # L'art. 389 n'exige ni numéro, ni tribunal, ni district, ni parties,
    # ni date : l'annexe B en marquait cinq « ✓ art. 389 ».
    arret = tax.SOUS_NATURES["JUG_ARRET"]
    assert set(arret.champs) == {tax.CHAMP_DISPOSITIF, tax.CHAMP_AUTEUR}
    assert tax.CHAMP_NUMERO in arret.champs_possibles
    assert arret.champs_ancres is True


# ── Champs absents et pénalités ─────────────────────────────────────────


def _acte_complet() -> dict:
    return {
        tax.CHAMP_NUMERO: "500-17-123456-241",
        tax.CHAMP_TRIBUNAL: "Cour supérieure",
        tax.CHAMP_DISTRICT: "Montréal",
        tax.CHAMP_PARTIES: ["Tremblay", "Lavoie"],
        tax.CHAMP_DATE: "2026-03-04",
        tax.CHAMP_AUTEUR: "Me Jason Poirier Lavoie",
    }


def test_acte_de_procedure_complet_n_a_aucun_champ_absent():
    absents = prot.champs_attendus_absents("PROC_DEFENSE", _acte_complet())
    assert absents == ()


def test_champs_absents_vide_pour_une_photo():
    """Le test le plus important de la série (§14).

    Les champs de l'art. 99 sont NULS sur une photographie, et cette
    absence est NORMALE pour la famille : rien ne doit être signalé, et
    rien ne doit être inventé.
    """
    assert prot.champs_attendus_absents("PREUVE_PHOTO", {}) == ()
    assert prot.champs_attendus_absents("PREUVE_CONTRAT", {}) == ()
    assert prot.champs_attendus_absents("PIECE_COMMUNIQUEE", {}) == ()


def test_un_champ_blanc_compte_comme_absent():
    doc = {**_acte_complet(), tax.CHAMP_NUMERO: "   "}
    assert tax.CHAMP_NUMERO in prot.champs_attendus_absents("PROC_DEFENSE", doc)


def test_les_champs_absents_gardent_l_ordre_de_la_matrice():
    absents = prot.champs_attendus_absents("PROC_DEFENSE", {})
    assert absents == tax._ART_99


def test_l_absence_de_numero_ne_penalise_pas_une_demande_introductive():
    # Art. 107 : c'est le greffier qui attribue le numéro AU DÉPÔT. Une
    # demande introductive n'en porte donc pas avant, et c'est l'état
    # normal — pas un défaut.
    doc = {**_acte_complet()}
    del doc[tax.CHAMP_NUMERO]
    absents = prot.champs_attendus_absents("PROC_DEM_INTRO", doc)
    assert absents == (tax.CHAMP_NUMERO,)
    assert prot.champs_penalisants("PROC_DEM_INTRO", absents) == ()
    # …mais sur une défense, la même absence pèse.
    absents = prot.champs_attendus_absents("PROC_DEFENSE", doc)
    assert prot.champs_penalisants("PROC_DEFENSE", absents) == (tax.CHAMP_NUMERO,)


def test_une_ligne_non_ancree_ne_penalise_rien():
    # « usage — non vérifié » a enfin un effet mécanique.
    absents = prot.champs_attendus_absents("PV_AUDIENCE", {},
                                           contient_dispositif=False)
    assert absents  # elles sont bien signalées…
    assert prot.champs_penalisants("PV_AUDIENCE", absents,
                                   contient_dispositif=False) == ()


# ── §6.3 / §6.4 — le régime de protection ───────────────────────────────


def _regime(**kw):
    base = dict(
        nature="procédure",
        sous_nature="PROC_DEFENSE",
        privileges=(),
        champs_absents=(),
        domaine_dossier="REC",
    )
    base.update(kw)
    return prot.appliquer_regime(**base)


def test_niveau_protection_liste_vide_est_none_jamais_zero():
    """Le test le plus important de toute la phase (§6.3 règle 4)."""
    assert prot.niveau_protection(()) is None
    assert prot.niveau_protection(None) is None
    assert prot.niveau_protection(("CODE_INCONNU",)) is None
    assert prot.niveau_protection(("PUBLIC",)) == 0


def test_niveau_protection_est_le_maximum():
    assert prot.niveau_protection(("PUBLIC", "CONFIDENTIEL")) == 1
    assert prot.niveau_protection(("LITIGE", "REGLEMENT")) == 2


def test_cumul_litige_et_secret_donne_trois():
    # §14 : un mémorandum interne préparatoire est couvert par les DEUX.
    codes, niveau, _ = _regime(
        nature="autre", sous_nature="CAB_MEMO", domaine_dossier="REC"
    )
    assert set(codes) >= {"LITIGE", "SECRET_PROFESSIONNEL"}
    assert niveau == 3


def test_acte_de_procedure_depose_est_public():
    codes, niveau, motifs = _regime(
        privileges=("PUBLIC",), champs_absents=()
    )
    assert codes == ("PUBLIC",)
    assert niveau == 0
    assert prot.MOTIF_PROJET_NON_DEPOSE not in motifs


def test_procedure_sans_numero_ne_peut_pas_etre_public():
    # Même en AFFIRMANT PUBLIC, le modèle est écarté : le document n'a
    # jamais accédé au caractère public de l'art. 11.
    codes, niveau, motifs = _regime(
        privileges=("PUBLIC",), champs_absents=("numero_dossier_cour",)
    )
    assert "PUBLIC" not in codes
    assert "LITIGE" in codes
    assert niveau == 2
    assert prot.MOTIF_PROJET_NON_DEPOSE in motifs


def test_correspondance_au_client_est_secret_professionnel():
    codes, niveau, _ = _regime(
        nature="correspondance", sous_nature="CORR_CLIENT"
    )
    assert "SECRET_PROFESSIONNEL" in codes
    assert niveau == 3


def test_lettre_sous_toutes_reserves_est_reglement():
    codes, niveau, _ = _regime(
        nature="correspondance",
        sous_nature="CORR_CONFRERE",
        privileges=("REGLEMENT",),
    )
    assert "REGLEMENT" in codes
    assert niveau == 2


def test_rapport_d_expert_non_communique_est_litige():
    codes, niveau, _ = _regime(
        nature="preuve", sous_nature="PREUVE_RAPPORT_EXPERT"
    )
    assert "LITIGE" in codes
    assert niveau == 2


def test_enquete_interne_entraine_litige():
    codes, _, _ = _regime(privileges=("ENQUETE_INTERNE",))
    assert "LITIGE" in codes


def test_defaut_residuel_est_confidentiel_jamais_public():
    codes, niveau, motifs = _regime(
        nature="preuve", sous_nature="PREUVE_AUTRE"
    )
    assert codes == ("CONFIDENTIEL",)
    assert niveau == 1
    assert prot.MOTIF_DEFAUT_RESIDUEL in motifs


# ── Art. 16 C.p.c. — l'accès restreint, et le trou des dossiers hérités ──


def test_domaine_familial_rabat_public_sur_confidentiel():
    codes, niveau, motifs = _regime(
        nature="jugement",
        sous_nature="JUG_JUGEMENT",
        domaine_dossier="FAM",
    )
    assert "PUBLIC" not in codes
    assert "CONFIDENTIEL" in codes
    assert niveau == 1
    assert prot.MOTIF_ACCES_RESTREINT_DOMAINE in motifs


def test_un_numero_de_chambre_familiale_rabat_sans_aucun_domaine():
    """Le trou qui échoue OUVERT, refermé.

    `_MATTER_TYPE_TO_DOMAINE` mappe délibérément « familial » → "" : un
    dossier familial d'avant la taxonomie porte donc un domaine VIDE, et un
    prédicat lisant seulement `domaine == "FAM"` ne se déclencherait pas sur
    exactement la population que l'art. 16 protège.
    """
    for code in sorted(tax.JURIDICTIONS_ACCES_RESTREINT):
        codes, niveau, motifs = _regime(
            nature="jugement",
            sous_nature="JUG_JUGEMENT",
            domaine_dossier="",
            numero_dossier_extrait=f"500-{code}-123456-241",
        )
        assert "PUBLIC" not in codes, code
        assert prot.MOTIF_ACCES_RESTREINT_NUMERO in motifs, code
        assert niveau == 1, code


def test_le_numero_du_dossier_parent_declenche_aussi():
    _codes, _n, motifs = _regime(
        nature="jugement",
        sous_nature="JUG_JUGEMENT",
        domaine_dossier="REC",
        numero_dossier_du_dossier="500-12-999999-241",
    )
    assert prot.MOTIF_ACCES_RESTREINT_NUMERO in motifs


def test_un_domaine_indetermine_n_autorise_jamais_public():
    # On ne peut pas ÉTABLIR qu'un dossier non classé n'est pas familial.
    codes, niveau, motifs = _regime(
        nature="jugement", sous_nature="JUG_JUGEMENT", domaine_dossier=""
    )
    assert "PUBLIC" not in codes
    assert niveau == 1
    assert prot.MOTIF_DOMAINE_INDETERMINE in motifs


def test_une_juridiction_non_familiale_laisse_public():
    codes, niveau, _ = _regime(
        nature="jugement",
        sous_nature="JUG_JUGEMENT",
        domaine_dossier="REC",
        numero_dossier_extrait="500-17-123456-241",
    )
    assert codes == ("PUBLIC",)
    assert niveau == 0


# ── §5.6 / §6.5 — la pièce et la renonciation ───────────────────────────


def test_une_piece_cotee_sans_indice_est_publique():
    codes, niveau, _ = _regime(
        nature="pièce", sous_nature="PIECE_COMMUNIQUEE", domaine_dossier="REC"
    )
    assert codes == ("PUBLIC",)
    assert niveau == 0


def test_une_piece_privilegiee_ne_devient_pas_publique():
    """§5.6 contredisait §6.5 : la cotation rend la pièce PRÉSOMPTIVEMENT
    publique, et le défaut ne s'applique donc pas quand un régime protégé a
    été observé — sans quoi l'étiquette et l'alerte se contrediraient sur
    le même document."""
    codes, niveau, motifs = _regime(
        nature="pièce",
        sous_nature="PIECE_COMMUNIQUEE",
        privileges=("SECRET_PROFESSIONNEL",),
        domaine_dossier="REC",
    )
    assert "PUBLIC" not in codes
    assert niveau == 3
    assert prot.MOTIF_PRESOMPTION_PUBLIQUE_ECARTEE in motifs
    assert prot.alerte_renonciation_possible("pièce", codes) is True


def test_l_alerte_de_renonciation_ignore_le_simple_confidentiel():
    # Toute pièce était confidentielle avant son dépôt : ce serait du bruit,
    # et une alerte qu'on écarte chaque jour cesse d'être lue.
    assert prot.alerte_renonciation_possible("pièce", ("CONFIDENTIEL",)) is False
    assert prot.alerte_renonciation_possible("pièce", ("PUBLIC",)) is False
    assert prot.alerte_renonciation_possible("pièce", ("LITIGE",)) is True
    assert prot.alerte_renonciation_possible("preuve", ("LITIGE",)) is False


# ── La divergence de numéro (ajout hors spec) ───────────────────────────


def test_divergence_de_numero_de_dossier():
    assert prot.divergence_numero_dossier(
        "500-17-123456-241", "500-17-999999-241"
    ) is True
    # Les tirets et espaces d'une transcription à la main ne sont pas une
    # divergence.
    assert prot.divergence_numero_dossier(
        "500-17-123456-241", "500 17 123456 241"
    ) is False
    # Une valeur manquante n'est pas une divergence.
    assert prot.divergence_numero_dossier("", "500-17-123456-241") is False
    assert prot.divergence_numero_dossier("500-17-123456-241", "") is False


# ── Ce que le module ne doit PAS faire ──────────────────────────────────


def test_aucun_calcul_de_delai_dans_la_couche_pure():
    """§17 : détecter qu'un PV porte un jugement est un fait opérationnel
    — un jugement rendu à l'audience fait courir les délais d'appel — mais
    fonder un délai de rigueur sur une lecture automatique demanderait une
    fiabilité que rien n'a établie."""
    from pathlib import Path

    racine = Path(__file__).resolve().parent.parent / "utils"
    for nom in ("analyse_taxonomies.py", "analyse_protection.py"):
        src = (racine / nom).read_text(encoding="utf-8")
        for interdit in ("import deadlines", "from utils.deadlines",
                         "import protocol", "compute_deadline"):
            assert interdit not in src, f"{nom}: {interdit}"


def test_les_modules_restent_firestore_free():
    """Le motif template_fields : ces tables sont importées par les schémas
    MCP et les gabarits, qui ne doivent pas construire le client."""
    from pathlib import Path

    racine = Path(__file__).resolve().parent.parent / "utils"
    for nom in ("analyse_taxonomies.py", "analyse_protection.py"):
        src = (racine / nom).read_text(encoding="utf-8")
        for interdit in ("from models", "import models", "firebase_admin"):
            assert interdit not in src, f"{nom}: {interdit}"
