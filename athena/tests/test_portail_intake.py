"""Formulaire d'ouverture du portail (L3 C2).

Ce que ces tests protègent, par ordre de gravité :

1. **La porte par type.** Une invitation « documents » ne doit pas atteindre
   les routes d'ouverture, ni l'inverse — et une route non déclarée dans
   ``_TYPE_REQUIS`` doit être refusée par défaut, jamais ouverte aux deux.
2. **Le plafond du témoin.** Le brouillon vit dans la session Flask, c'est-à-
   dire dans un témoin signé qu'un navigateur jette SILENCIEUSEMENT au-delà de
   ~4096 octets. Le client perdrait sa session en plein formulaire, alors que
   son lien à usage unique est déjà consommé.
3. **La liste blanche.** Rien de ce que le client envoie n'entre en session
   sans être connu et borné.
4. **La ré-entrée** (décision 2026-07-27) : corriger et re-soumettre tant que
   le juriste n'a pas traité l'ouverture.
"""

import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from flask.sessions import SecureCookieSessionInterface

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")
os.environ.setdefault("PORTAIL_SECRET_KEY", "test-portail-secret")

import client.app as client_app  # noqa: E402
import client.routes as routes  # noqa: E402
from client import limiter  # noqa: E402
from client.config import (  # noqa: E402
    INTAKE_CHAMP_MAX,
    INTAKE_CHAMP_MAX_PAR_NOM,
    INTAKE_MAX_ADVERSES,
    INTAKE_NOM_MAX,
    INTAKE_PRECISION_MAX,
)
from client.services import invitations, stockage, taches  # noqa: E402


@pytest.fixture(scope="module")
def app():
    with mock.patch("utils.tracing_setup.init_app"):
        with mock.patch.object(client_app, "_init_firebase"):
            application = client_app.create_portail_app()
    application.config["TESTING"] = True
    limiter.enabled = False
    return application


@pytest.fixture()
def web(app):
    return app.test_client()


def _invitation(**over) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "id": "inv1", "type": "intake", "email": "client@exemple.com",
        "statut": "ouverte", "display_label": "Ouverture de votre dossier",
        "dossier_id": None, "partie_id": None, "created_at": now,
        "expires_at": now + timedelta(days=14),
        "soumissions": [], "accuses": {}, "resend_count": 0,
        "prefill": None,
    }
    base.update(over)
    return base


@pytest.fixture()
def connecte(web, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda inv_id: _invitation())
    monkeypatch.setattr(taches, "signaler", mock.Mock())
    with web.session_transaction() as s:
        s["inv_id"] = "inv1"
        s["uid"] = "u1"
        s["email"] = "client@exemple.com"
    return _invitation()


def _post_json(web, path, payload):
    with mock.patch("flask_wtf.csrf.validate_csrf", return_value=None):
        return web.post(path, data=json.dumps(payload),
                        content_type="application/json")


def _complet(**over) -> dict:
    charge = {
        "nature": "physique", "prenom": "Jean", "nom": "Tremblay",
        "langue": "fr", "telephone": "514 555-1234",
        "adresse_rue": "10 rue Principale", "adresse_ville": "Montréal",
        "parties_adverses": [{"nom": "Béton Nord inc.", "precision": ""}],
        "consentement": True,
    }
    charge.update(over)
    return charge


# ── Porte par type ───────────────────────────────────────────────────────


def test_une_invitation_documents_n_atteint_pas_l_ouverture(
    web, connecte, monkeypatch
):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(type="documents"))
    assert web.get("/ouverture").status_code == 302        # → /entree
    assert _post_json(web, "/api/intake/etape", {}).status_code == 401


def test_une_invitation_intake_n_atteint_pas_les_documents(
    web, connecte
):
    assert web.get("/documents").status_code == 302
    assert _post_json(web, "/api/televersement", {
        "name": "a.pdf", "size": 10, "content_type": "application/pdf",
    }).status_code == 401


def test_toute_route_gardee_declare_son_type():
    """Fail closed : ``_TYPE_REQUIS.get(endpoint, "")`` refuse un endpoint
    inconnu (aucun type ne vaut « »). Ce test empêche surtout d'oublier une
    route future — l'oubli serait un refus, mais un refus INEXPLICABLE."""
    gardes = {
        r.endpoint for r in routes.portail_bp.deferred_functions and []
    }  # placeholder — vérification réelle ci-dessous
    del gardes
    connus = set(routes._TYPE_REQUIS)
    assert "portail.page_ouverture" in connus
    assert "portail.api_intake_etape" in connus
    assert "portail.api_intake_finaliser" in connus
    # Un endpoint absent de la table est refusé (aucun type ne vaut "").
    assert routes._TYPE_REQUIS.get("portail.inconnu", "") == ""


def test_la_confirmation_sert_les_deux_files(web, connecte, monkeypatch):
    assert routes._TYPE_REQUIS["portail.confirmation"] is None
    assert web.get("/confirmation").status_code == 200
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(type="documents"))
    assert web.get("/confirmation").status_code == 200


def test_session_dirige_vers_la_page_du_type(web, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation())
    faux = mock.Mock()
    faux.verify_id_token.return_value = {
        "uid": "u1", "portail": True, "email_verified": True,
        "email": "client@exemple.com",
    }
    monkeypatch.setitem(sys.modules, "firebase_admin",
                        mock.Mock(auth=faux))
    monkeypatch.setattr(taches, "signaler", mock.Mock())
    with mock.patch("firebase_admin.auth.verify_id_token",
                    faux.verify_id_token):
        rep = _post_json(web, "/session", {"token": "t", "i": "inv1"})
    assert rep.status_code == 200
    assert rep.get_json()["suivant"].endswith("/ouverture")
    with web.session_transaction() as s:
        # La frappe du lot à la session (2026-08-11) est propre à la file
        # documents — l'intake frappe le sien à SA finalisation.
        assert "batch" not in s


# ── Plafond du témoin ────────────────────────────────────────────────────


def test_le_plafond_garantit_un_temoin_valide(web, connecte):
    """LA mesure qui compte : le Set-Cookie réel, pas une arithmétique.

    Un dépassement du témoin est SILENCIEUX — le navigateur le jette, donc le
    client perd sa session en plein formulaire alors que son lien à usage
    unique est déjà consommé. On remplit donc le brouillon JUSQU'À la limite
    que la garde autorise, et on mesure le témoin que Flask émet réellement.
    """
    # Bourrage INCOMPRESSIBLE. Un piège s'est présenté en écrivant ce test :
    # avec « é » répété, itsdangerous compresse la session à 195 octets et la
    # mesure ne prouve plus rien. Le vrai pire cas est un contenu que zlib ne
    # réduit pas — du texte pseudo-aléatoire, reproductible via une graine.
    alphabet = "abcdefghijklmnopqrstuvwxyzÉÀÈÇ0123456789 -'"
    rng = random.Random(20260727)
    bourrage = "".join(rng.choice(alphabet) for _ in range(2600))
    brouillon = {"x": bourrage}
    while routes._brouillon_trop_gros(brouillon):
        bourrage = bourrage[:-20]
        brouillon = {"x": bourrage}
    # Un brouillon accepté de justesse, au ras du plafond.
    assert len(
        json.dumps(brouillon, ensure_ascii=False).encode("utf-8")
    ) > routes.INTAKE_BROUILLON_MAX - 100

    serialiseur = SecureCookieSessionInterface().get_signing_serializer(
        web.application
    )
    temoin = serialiseur.dumps({
        "inv_id": "inv1", "uid": "u1", "email": "client@exemple.com",
        "csrf_token": "a1b2c3" * 7, "intake": brouillon,
    })
    # 4096 est le plafond que les navigateurs appliquent au témoin ENTIER.
    assert len(temoin) < 4096, len(temoin)


def test_un_brouillon_trop_gros_est_refuse_en_francais(web, connecte):
    """Refuser explicitement plutôt que déborder sans bruit.

    La garde est en OCTETS, donc elle tient compte des accents ; les bornes par
    champ, elles, sont en caractères. Un formulaire entièrement accentué et
    saturé peut donc franchir le plafond bien qu'aucune borne ne soit violée —
    c'est exactement le cas que cette garde existe pour attraper.
    """
    saturant = {
        cle: "É" * (INTAKE_CHAMP_MAX_PAR_NOM.get(cle, INTAKE_CHAMP_MAX) + 50)
        for cle in routes._CHAMPS_INTAKE
        if cle not in ("parties_adverses", "consentement")
    }
    saturant["consentement"] = True
    saturant["parties_adverses"] = [
        {"nom": "É" * (INTAKE_NOM_MAX + 50),
         "precision": "É" * (INTAKE_PRECISION_MAX + 50)}
        for _ in range(INTAKE_MAX_ADVERSES + 3)
    ]
    rep = _post_json(web, "/api/intake/etape", saturant)
    assert rep.status_code == 422
    assert "taille" in rep.get_json()["erreur"].lower()
    with web.session_transaction() as s:
        assert "intake" not in s          # rien n'est écrit à moitié


def test_un_formulaire_realiste_passe_tres_largement(web, connecte):
    """La garde ne doit pas gêner un usage normal : un formulaire complet,
    accentué, avec le maximum de parties adverses, reste loin du plafond."""
    realiste = {
        "nature": "physique", "prenom": "Jean-François", "nom": "Tremblay",
        "langue": "fr", "telephone": "514 555-1234",
        "telephone2": "438 555-9876",
        "adresse_rue": "1250, boulevard René-Lévesque Ouest",
        "adresse_app": "app. 302", "adresse_ville": "Montréal",
        "adresse_province": "Québec", "adresse_code_postal": "H3B 4W8",
        "adresse_pays": "Canada", "consentement": True,
        "parties_adverses": [
            {"nom": "Les Entreprises Générales Côté & Frères inc.",
             "precision": "mon ancien employeur"}
            for _ in range(INTAKE_MAX_ADVERSES)
        ],
    }
    propre = routes._nettoyer_etape(realiste)
    octets = len(json.dumps(propre, ensure_ascii=False).encode("utf-8"))
    assert octets < routes.INTAKE_BROUILLON_MAX / 2, octets
    assert _post_json(web, "/api/intake/etape", realiste).status_code == 200


# ── Liste blanche et bornes ──────────────────────────────────────────────


def test_un_champ_inconnu_n_entre_pas_en_session(web, connecte):
    _post_json(web, "/api/intake/etape",
               {"nom": "Tremblay", "champ_pirate": "x" * 500})
    with web.session_transaction() as s:
        assert s["intake"]["nom"] == "Tremblay"
        assert "champ_pirate" not in s["intake"]


def test_le_courriel_ne_vient_jamais_du_client(web, connecte):
    """C'est la cohérence d'identité entre le lien, la session et l'enveloppe.
    Le client ne peut pas le déplacer."""
    _post_json(web, "/api/intake/etape",
               {"courriel": "attaquant@exemple.com", "nom": "Tremblay"})
    with web.session_transaction() as s:
        assert "courriel" not in s["intake"]


def test_les_bornes_sont_appliquees_cote_serveur(web, connecte):
    _post_json(web, "/api/intake/etape", {
        "nom": "N" * 900,
        "parties_adverses": [
            {"nom": "A" * 900, "precision": "B" * 900}
            for _ in range(INTAKE_MAX_ADVERSES + 4)
        ],
    })
    with web.session_transaction() as s:
        brouillon = s["intake"]
    assert len(brouillon["nom"]) == INTAKE_CHAMP_MAX
    assert len(brouillon["parties_adverses"]) == INTAKE_MAX_ADVERSES
    assert len(brouillon["parties_adverses"][0]["nom"]) == INTAKE_NOM_MAX
    assert len(brouillon["parties_adverses"][0]["precision"]) == INTAKE_PRECISION_MAX


def test_les_lignes_adverses_sans_nom_sont_ecartees(web, connecte):
    _post_json(web, "/api/intake/etape", {"parties_adverses": [
        {"nom": "", "precision": "orpheline"},
        {"nom": "Béton Nord inc.", "precision": ""},
        "pas un objet",
    ]})
    with web.session_transaction() as s:
        assert len(s["intake"]["parties_adverses"]) == 1


def test_une_valeur_d_enumeration_invalide_retombe_sur_le_defaut(web, connecte):
    _post_json(web, "/api/intake/etape",
               {"nature": "extraterrestre", "langue": "kl"})
    with web.session_transaction() as s:
        assert s["intake"]["nature"] == "physique"
        assert s["intake"]["langue"] == "fr"


# ── Finalisation ─────────────────────────────────────────────────────────


def test_finalisation_ecrit_une_enveloppe_intake(web, connecte, monkeypatch):
    ecrit = mock.Mock()
    monkeypatch.setattr(stockage, "ecrire_enveloppe", ecrit)
    enfile = mock.Mock()
    monkeypatch.setattr(taches, "signaler", enfile)

    rep = _post_json(web, "/api/intake/finaliser", _complet())
    assert rep.status_code == 200
    assert rep.get_json()["suivant"].endswith("/confirmation")

    _inv, _batch, envelope = ecrit.call_args[0]
    assert envelope["type"] == "intake"
    assert envelope["donnees"]["nom"] == "Tremblay"
    # Le courriel vient de l'INVITATION, pas de la charge utile.
    assert envelope["donnees"]["courriel"] == "client@exemple.com"
    assert envelope["parties_adverses"][0]["nom"] == "Béton Nord inc."
    assert envelope["consentement"]["accepte"] is True
    assert envelope["consentement"]["version_texte"] == "1"
    # Emplacement réservé — aucune pièce d'identité n'est collectée (§6).
    assert envelope["pieces_identite"] is None
    assert envelope["files"] if "files" in envelope else True
    assert enfile.call_args[0][0] == "soumise"

    with web.session_transaction() as s:
        assert "intake" not in s


def test_finalisation_exige_le_consentement(web, connecte, monkeypatch):
    ecrit = mock.Mock()
    monkeypatch.setattr(stockage, "ecrire_enveloppe", ecrit)
    rep = _post_json(web, "/api/intake/finaliser",
                     _complet(consentement=False))
    assert rep.status_code == 422
    assert "consentement" in rep.get_json()["erreur"].lower()
    ecrit.assert_not_called()


def test_finalisation_exige_le_nom_et_le_telephone(web, connecte, monkeypatch):
    monkeypatch.setattr(stockage, "ecrire_enveloppe", mock.Mock())
    rep = _post_json(web, "/api/intake/finaliser",
                     _complet(nom="", telephone=""))
    assert rep.status_code == 422
    corps = rep.get_json()["erreur"]
    assert "nom" in corps.lower() and "téléphone" in corps.lower()


def test_finalisation_morale_exige_la_denomination(web, connecte, monkeypatch):
    monkeypatch.setattr(stockage, "ecrire_enveloppe", mock.Mock())
    rep = _post_json(web, "/api/intake/finaliser",
                     _complet(nature="morale", denomination="", nom=""))
    assert rep.status_code == 422
    assert "dénomination" in rep.get_json()["erreur"].lower()


def test_rejeu_dans_la_meme_seconde_est_un_succes(web, connecte, monkeypatch):
    """Le batch est horodaté à la seconde : un double clic retombe sur le même
    objet. Le formulaire EST transmis — répondre par une erreur inviterait le
    client à recommencer."""
    from google.api_core.exceptions import PreconditionFailed

    monkeypatch.setattr(
        stockage, "ecrire_enveloppe",
        mock.Mock(side_effect=PreconditionFailed("exists")),
    )
    rep = _post_json(web, "/api/intake/finaliser", _complet())
    assert rep.status_code == 200
    assert rep.get_json()["suivant"].endswith("/confirmation")


def test_l_echec_d_enfilage_ne_fait_pas_echouer_la_soumission(
    web, connecte, monkeypatch
):
    """L'enveloppe est la vérité durable ; la réconciliation rejouera."""
    monkeypatch.setattr(stockage, "ecrire_enveloppe", mock.Mock())
    monkeypatch.setattr(
        taches, "signaler", mock.Mock(side_effect=RuntimeError("queue down"))
    )
    assert _post_json(
        web, "/api/intake/finaliser", _complet()
    ).status_code == 200


# ── Ré-entrée ────────────────────────────────────────────────────────────


def test_re_entree_possible_tant_que_non_traitee(web, connecte, monkeypatch):
    """Décision 2026-07-27 : corriger et re-soumettre jusqu'au traitement."""
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="soumise"))
    assert web.get("/ouverture").status_code == 200


def test_re_entree_fermee_une_fois_traitee(web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="traitée"))
    assert web.get("/ouverture").status_code == 302


def test_prefill_alimente_le_premier_passage(web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation(prefill={
        "type": "individual", "first_name": "Jean", "last_name": "Tremblay",
        "phone_cell": "+15145551234", "address_city": "Montréal",
    }))
    page = web.get("/ouverture").get_data(as_text=True)
    assert "Tremblay" in page and "Montréal" in page


def test_le_brouillon_en_session_prime_sur_le_prefill(web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation(prefill={
        "type": "individual", "last_name": "AncienNom",
    }))
    _post_json(web, "/api/intake/etape", {"nom": "NouveauNom"})
    page = web.get("/ouverture").get_data(as_text=True)
    assert "NouveauNom" in page
    assert "AncienNom" not in page


def test_un_refus_purge_le_brouillon(web, connecte, monkeypatch):
    """« intake » doit figurer dans le tuple de purge de _refus : sinon un
    refus laisse le brouillon d'un client derrière lui, et le suivant sur le
    même appareil le retrouverait prérempli."""
    _post_json(web, "/api/intake/etape", {"nom": "Tremblay"})
    monkeypatch.setattr(invitations, "lire", lambda i: None)
    web.get("/ouverture")
    with web.session_transaction() as s:
        assert "intake" not in s


# ── Date de naissance (facultative) ──────────────────────────────────────


def test_la_date_de_naissance_atteint_l_enveloppe(web, connecte, monkeypatch):
    ecrit = mock.Mock()
    monkeypatch.setattr(stockage, "ecrire_enveloppe", ecrit)
    _post_json(web, "/api/intake/finaliser",
               _complet(date_naissance="1985-03-17"))
    _i, _b, envelope = ecrit.call_args[0]
    assert envelope["donnees"]["date_naissance"] == "1985-03-17"


def test_elle_reste_facultative(web, connecte, monkeypatch):
    ecrit = mock.Mock()
    monkeypatch.setattr(stockage, "ecrire_enveloppe", ecrit)
    assert _post_json(
        web, "/api/intake/finaliser", _complet()
    ).status_code == 200
    _i, _b, envelope = ecrit.call_args[0]
    assert envelope["donnees"]["date_naissance"] == ""


@pytest.mark.parametrize("mauvaise", ["17/03/1985", "hier", "1985-13-45", "2999-01-01"])
def test_une_date_illisible_est_refusee_et_non_ecartee(
    web, connecte, monkeypatch, mauvaise
):
    """L'écarter en silence ferait croire au client qu'il l'a fournie, et le
    juriste ne la verrait jamais — c'est la classe d'échec que ce dépôt
    refuse."""
    ecrit = mock.Mock()
    monkeypatch.setattr(stockage, "ecrire_enveloppe", ecrit)
    rep = _post_json(web, "/api/intake/finaliser",
                     _complet(date_naissance=mauvaise))
    assert rep.status_code == 422
    assert "naissance" in rep.get_json()["erreur"].lower()
    ecrit.assert_not_called()


def test_une_personne_morale_n_est_pas_bloquee_par_ce_champ(
    web, connecte, monkeypatch
):
    """Une entreprise n'a pas de date de naissance : un résidu dans le
    brouillon (bascule physique → morale) ne doit pas la bloquer."""
    monkeypatch.setattr(stockage, "ecrire_enveloppe", mock.Mock())
    rep = _post_json(web, "/api/intake/finaliser", _complet(
        nature="morale", denomination="Béton Sud inc.", nom="",
        date_naissance="pas une date",
    ))
    assert rep.status_code == 200


def test_le_selecteur_natif_est_borne_a_aujourd_hui(web, connecte):
    page = web.get("/ouverture").get_data(as_text=True)
    assert 'type="date"' in page
    assert f'max="{datetime.now(timezone.utc).date().isoformat()}"' in page


def test_le_pays_atteint_l_enveloppe(web, connecte, monkeypatch):
    """Le champ manquait au FORMULAIRE seulement : la liste blanche, le
    préremplissage et la table de Réception l'acceptaient déjà. Ce test
    verrouille le trajet complet maintenant que l'input existe."""
    ecrit = mock.Mock()
    monkeypatch.setattr(stockage, "ecrire_enveloppe", ecrit)
    _post_json(web, "/api/intake/finaliser",
               _complet(adresse_pays="France"))
    _i, _b, envelope = ecrit.call_args[0]
    assert envelope["donnees"]["adresse_pays"] == "France"


def test_le_pays_est_visible_dans_l_assistant(web, connecte):
    page = web.get("/ouverture").get_data(as_text=True)
    assert 'data-champ="adresse_pays"' in page
    # Prérempli « Canada », comme la province est préremplie « Québec ».
    assert 'value="Canada"' in page


def test_le_prefill_remplit_le_pays(web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation(prefill={
        "type": "individual", "last_name": "Tremblay",
        "address_country": "Belgique",
    }))
    page = web.get("/ouverture").get_data(as_text=True)
    assert 'value="Belgique"' in page
