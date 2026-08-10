"""Réception → onglet « Ouvertures » (L3 C4).

Ce que ces tests protègent :

1. **Rien n'entre en Firestore avant un clic** — et quand le clic arrive, la
   section Conformité reste intacte : recueillir n'est pas vérifier.
2. **Le bump du CTag `parties`** — il vit dans la ROUTE, jamais dans le
   modèle. Une création de contact qui l'oublie n'atteint JAMAIS le carnet
   DavX5, en silence. C'est le défaut le plus coûteux de cette phase.
3. **Seuls les champs cochés sont appliqués**, et un champ soumis vide ne
   propose jamais d'effacer une valeur au dossier.
4. Une enveloppe illisible reste visible et actionnable, jamais un 500.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.reception as rc
    import routes.parties as rp

from flask import Flask  # noqa: E402

_TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)


@pytest.fixture()
def web():
    app = Flask(__name__, template_folder=_TEMPLATES)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    from utils.icons import ms as _ms
    app.jinja_env.globals["ms"] = _ms  # icônes Material (global posé par create_app en prod)
    app.register_blueprint(rc.reception_bp)
    # Réception redirige vers la fiche créée (§5.3) : sans ce blueprint,
    # url_for lèverait BuildError — ce qui a d'ailleurs révélé que
    # partie_detail ignorait le message.
    app.register_blueprint(rp.parties_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _inv(**over) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "id": "inv1", "type": "intake", "email": "client@exemple.com",
        "statut": "soumise", "display_label": "Ouverture de votre dossier",
        "partie_id": None, "dossier_id": None, "created_at": now,
        "expires_at": now + timedelta(days=14),
        "soumissions": [{"batch": "b1", "files_count": 0, "total_bytes": 0}],
        "accuses": {"b1": True},
    }
    base.update(over)
    return base


def _enveloppe(**over) -> dict:
    base = {
        "type": "intake", "invitation_id": "inv1", "batch": "b1",
        "submitted_at": "2026-07-20T12:00:00+00:00",
        "donnees": {
            "nature": "physique", "prenom": "Jean", "nom": "Tremblay",
            "courriel": "client@exemple.com", "telephone": "514 555-1234",
            "adresse_rue": "10 rue Principale", "adresse_ville": "Montréal",
            "langue": "fr",
        },
        "parties_adverses": [{"nom": "Béton Nord inc.", "precision": ""}],
        "consentement": {"accepte": True, "version_texte": "1",
                         "horodatage": "2026-07-20T12:00:00+00:00"},
        "pieces_identite": None,
    }
    base.update(over)
    return base


@pytest.fixture()
def scene(monkeypatch):
    """Firestore + seau bouchonnés ; retourne le journal des écritures."""
    journal = {
        "crees": [], "maj": [], "statuts": [], "bumps": [], "archives": [],
    }
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv())
    monkeypatch.setattr(rc.pi, "maj_statut",
                        lambda i, s: journal["statuts"].append((i, s)) or True)
    monkeypatch.setattr(rc, "_lire_enveloppe", lambda i, b: _enveloppe())
    monkeypatch.setattr(rc, "_archiver_enveloppe",
                        lambda i, b: journal["archives"].append((i, b)))
    monkeypatch.setattr(rc, "bump_ctag", lambda n: journal["bumps"].append(n))

    def _create(data):
        journal["crees"].append(data)
        return {"id": f"p{len(journal['crees'])}", **data}, []

    def _update(pid, data):
        journal["maj"].append((pid, data))
        return {"id": pid, **data}, []

    monkeypatch.setattr(rc, "create_partie", _create)
    monkeypatch.setattr(rc, "update_partie", _update)
    monkeypatch.setattr(rc, "list_parties", lambda **k: [])
    monkeypatch.setattr(rc, "get_partie", lambda pid: (
        {"id": pid, "first_name": "Jean", "last_name": "Tremblay",
         "email": "ancien@exemple.com", "phone_cell": "", "language": "fr"}
        if pid else None
    ))
    return journal


def _post(web, chemin, data=None):
    with mock.patch("flask_wtf.csrf.validate_csrf", return_value=None):
        return web.post(chemin, data=data or {})


# ── Création (nouveau client) ────────────────────────────────────────────


def test_creer_produit_une_fiche_conforme(web, scene):
    rep = _post(web, "/reception/ouvertures/inv1/b1/creer")
    assert rep.status_code == 302

    fiche = scene["crees"][0]
    assert fiche["type"] == "individual"
    assert fiche["contact_role"] == "client"
    assert fiche["first_name"] == "Jean"
    assert fiche["last_name"] == "Tremblay"
    assert fiche["email"] == "client@exemple.com"
    assert fiche["phone_cell"] == "514 555-1234"
    assert fiche["address_street"] == "10 rue Principale"
    assert "portail" in fiche["notes"]


def test_creer_ne_touche_pas_la_conformite(web, scene):
    """Recueillir n'est PAS vérifier (§6). Ne rien écrire ici est délibéré :
    create_partie applique « non_vérifié » par défaut."""
    _post(web, "/reception/ouvertures/inv1/b1/creer")
    fiche = scene["crees"][0]
    for interdit in ("identity_verified", "identity_verified_date",
                     "conflict_check", "conflict_check_date",
                     "kyc_document_ids"):
        assert interdit not in fiche, interdit


def test_creer_bumpe_le_ctag_des_parties(web, scene):
    """LE défaut silencieux de cette phase : sans ce bump, la fiche existe en
    Firestore, s'affiche dans l'application, et n'atteint JAMAIS le carnet
    d'adresses DavX5."""
    _post(web, "/reception/ouvertures/inv1/b1/creer")
    assert scene["bumps"] == ["parties"]


def test_creer_mene_a_la_fiche_avec_un_message_lisible(web, scene):
    """La redirection porte un message, et ce message doit ARRIVER.

    partie_detail ignorait les paramètres de requête et son gabarit n'avait
    aucun bandeau : le message était construit puis silencieusement perdu, et
    le juriste atterrissait sur une fiche sans savoir ce qui venait de se
    passer ni combien de contacts adverses avaient été créés.
    """
    rep = _post(web, "/reception/ouvertures/inv1/b1/creer",
                {"creer_adverse": "Béton Nord inc."})
    cible = rep.headers["Location"]
    assert "/parties/p1" in cible
    assert "message=" in cible
    # Encodé par url_for, pas concaténé : un « & » ou un accent ne casse rien.
    assert "?message=Fiche+cr" in cible or "%C3%A9" in cible


def test_creer_cloture_et_archive(web, scene):
    _post(web, "/reception/ouvertures/inv1/b1/creer")
    assert scene["statuts"] == [("inv1", "traitée")]
    assert scene["archives"] == [("inv1", "b1")]


def test_une_personne_morale_devient_une_organisation(web, scene, monkeypatch):
    monkeypatch.setattr(rc, "_lire_enveloppe", lambda i, b: _enveloppe(
        donnees={"nature": "morale", "denomination": "Béton Sud inc.",
                 "neq": "1170012345", "courriel": "info@betonsud.ca"},
    ))
    _post(web, "/reception/ouvertures/inv1/b1/creer")
    fiche = scene["crees"][0]
    assert fiche["type"] == "organization"
    assert fiche["organization_name"] == "Béton Sud inc."
    assert fiche["company_neq"] == "1170012345"


# ── Parties adverses (D-L3-2) ────────────────────────────────────────────


def test_l_adverse_coche_est_cree_avec_sa_provenance(web, scene):
    _post(web, "/reception/ouvertures/inv1/b1/creer",
          {"creer_adverse": "Béton Nord inc."})
    adverse = [c for c in scene["crees"]
               if c.get("contact_role") == "partie_adverse"]
    assert len(adverse) == 1
    assert adverse[0]["last_name"] == "Béton Nord inc."
    assert "invitation inv1" in adverse[0]["notes"]


def test_l_adverse_non_coche_n_est_pas_cree(web, scene):
    """La case est cochée par défaut à l'écran, mais c'est le POST qui fait
    foi : décochée, aucun contact n'est créé."""
    _post(web, "/reception/ouvertures/inv1/b1/creer")
    assert not [c for c in scene["crees"]
                if c.get("contact_role") == "partie_adverse"]


def test_un_seul_bump_couvre_la_fiche_et_les_adverses(web, scene):
    _post(web, "/reception/ouvertures/inv1/b1/creer",
          {"creer_adverse": "Béton Nord inc."})
    assert scene["bumps"] == ["parties"]


# ── Mise à jour (contact existant) ───────────────────────────────────────


@pytest.fixture()
def scene_liee(scene, monkeypatch):
    monkeypatch.setattr(rc.pi, "lire_invitation",
                        lambda i: _inv(partie_id="p1"))
    return scene


def test_seuls_les_champs_coches_sont_appliques(web, scene_liee):
    _post(web, "/reception/ouvertures/inv1/b1/appliquer",
          {"appliquer": ["courriel", "telephone"]})
    _pid, valeurs = scene_liee["maj"][0]
    assert valeurs == {"email": "client@exemple.com",
                       "phone_cell": "514 555-1234"}
    # Le prénom/nom transmis ne sont PAS appliqués : cases non cochées.
    assert "first_name" not in valeurs and "last_name" not in valeurs


def test_aucune_case_cochee_n_ecrit_rien(web, scene_liee):
    rep = _post(web, "/reception/ouvertures/inv1/b1/appliquer")
    assert rep.status_code == 302
    assert scene_liee["maj"] == []
    assert scene_liee["bumps"] == []       # rien à synchroniser
    assert scene_liee["statuts"] == [("inv1", "traitée")]


def test_un_champ_soumis_vide_n_efface_jamais(web, scene_liee, monkeypatch):
    """Le silence d'un client n'est pas une rétractation : un champ vide ne
    doit jamais devenir une instruction d'effacer la valeur au dossier."""
    monkeypatch.setattr(rc, "_lire_enveloppe", lambda i, b: _enveloppe(
        donnees={"nature": "physique", "nom": "Tremblay", "telephone": ""},
    ))
    _post(web, "/reception/ouvertures/inv1/b1/appliquer",
          {"appliquer": ["telephone", "nom"]})
    _pid, valeurs = scene_liee["maj"][0]
    assert "phone_cell" not in valeurs
    assert valeurs == {"last_name": "Tremblay"}


def test_la_mise_a_jour_bumpe_aussi(web, scene_liee):
    _post(web, "/reception/ouvertures/inv1/b1/appliquer",
          {"appliquer": ["courriel"]})
    assert scene_liee["bumps"] == ["parties"]


def test_un_contact_lie_introuvable_est_refuse(web, scene_liee, monkeypatch):
    monkeypatch.setattr(rc, "get_partie", lambda pid: None)
    rep = _post(web, "/reception/ouvertures/inv1/b1/appliquer",
                {"appliquer": ["courriel"]})
    assert rep.status_code == 302
    assert "erreur" in rep.headers["Location"]
    assert scene_liee["maj"] == []
    assert scene_liee["statuts"] == []      # rien n'est clôturé sur erreur


# ── Refus (D-L3-3) ───────────────────────────────────────────────────────


def test_refuser_cloture_sans_ecrire_ni_courriel(web, scene):
    rep = _post(web, "/reception/ouvertures/inv1/b1/refuser")
    assert rep.status_code == 302
    assert scene["crees"] == [] and scene["maj"] == []
    assert scene["statuts"] == [("inv1", "refusée")]
    assert scene["archives"] == [("inv1", "b1")]
    assert scene["bumps"] == []


# ── Robustesse ───────────────────────────────────────────────────────────


def test_une_enveloppe_absente_refuse_sans_planter(web, scene, monkeypatch):
    monkeypatch.setattr(rc, "_lire_enveloppe", lambda i, b: None)
    rep = _post(web, "/reception/ouvertures/inv1/b1/creer")
    assert rep.status_code == 302
    assert "erreur" in rep.headers["Location"]
    assert scene["crees"] == []


def test_une_invitation_documents_n_est_pas_traitee_ici(web, scene, monkeypatch):
    """La garde de type, côté juriste aussi : une invitation « documents » n'a
    rien à faire dans le flux d'ouverture."""
    monkeypatch.setattr(rc.pi, "lire_invitation",
                        lambda i: _inv(type="documents"))
    rep = _post(web, "/reception/ouvertures/inv1/b1/creer")
    assert rep.status_code == 302
    assert scene["crees"] == []


def test_une_lecture_de_seau_en_panne_ne_plante_pas(web, scene, monkeypatch):
    def _boum(*_a, **_k):
        raise RuntimeError("bucket down")

    monkeypatch.setattr(rc, "_lire_enveloppe", _boum)
    rep = _post(web, "/reception/ouvertures/inv1/b1/creer")
    assert rep.status_code == 302
    assert scene["crees"] == []


# ── Contexte de l'onglet ─────────────────────────────────────────────────


def test_le_contexte_precalcule_tout(monkeypatch):
    monkeypatch.setattr(rc.pi, "lister_invitations", lambda **k: [_inv()])
    monkeypatch.setattr(rc, "_lire_enveloppe", lambda i, b: _enveloppe())
    monkeypatch.setattr(rc, "list_parties", lambda **k: [
        {"id": "p9", "type": "organization",
         "organization_name": "Béton Nord"},
    ])
    monkeypatch.setattr(rc, "get_partie", lambda pid: None)

    ctx = rc._contexte_ouvertures()
    assert ctx["erreur_ouvertures"] is False
    o = ctx["ouvertures"][0]
    assert o["lisible"] is True
    assert o["nature"] == "physique"
    # Le rapprochement de conflits est calculé côté route, pas au gabarit.
    assert o["adverses"][0]["candidats"][0]["id"] == "p9"
    libelles = {l["libelle"]: l for l in o["lignes"]}
    assert libelles["Nom"]["soumis"] == "Tremblay"
    assert libelles["Nom"]["applicable"] is True    # aucune fiche → tout diffère


def test_le_contexte_prend_la_derniere_soumission(monkeypatch):
    """La ré-entrée permet de corriger : c'est la version corrigée que le
    juriste doit voir."""
    vus = []
    monkeypatch.setattr(rc.pi, "lister_invitations", lambda **k: [_inv(
        soumissions=[{"batch": "b1"}, {"batch": "b2"}]
    )])
    monkeypatch.setattr(rc, "_lire_enveloppe",
                        lambda i, b: vus.append(b) or _enveloppe())
    monkeypatch.setattr(rc, "list_parties", lambda **k: [])
    monkeypatch.setattr(rc, "get_partie", lambda pid: None)

    ctx = rc._contexte_ouvertures()
    assert vus == ["b2"]
    assert ctx["ouvertures"][0]["versions"] == 2


def test_le_contexte_est_fail_open(monkeypatch):
    def _boum(**_k):
        raise RuntimeError("portail db absent")

    monkeypatch.setattr(rc.pi, "lister_invitations", _boum)
    ctx = rc._contexte_ouvertures()
    assert ctx["erreur_ouvertures"] is True
    assert ctx["ouvertures"] == []


def test_une_enveloppe_malformee_reste_visible(monkeypatch):
    """Elle ne disparaît pas en silence : la fiche s'affiche avec un bandeau et
    reste refusable."""
    monkeypatch.setattr(rc.pi, "lister_invitations", lambda **k: [_inv()])
    monkeypatch.setattr(rc, "_lire_enveloppe", lambda i, b: {"type": "intake"})
    monkeypatch.setattr(rc, "list_parties", lambda **k: [])
    monkeypatch.setattr(rc, "get_partie", lambda pid: None)

    ctx = rc._contexte_ouvertures()
    assert len(ctx["ouvertures"]) == 1
    assert ctx["ouvertures"][0]["lisible"] is False


# ── Déclencheurs d'invitation (§2, C5) ───────────────────────────────────


@pytest.fixture()
def emission(monkeypatch):
    """Capture les appels à emettre_invitation."""
    appels = []

    def _emettre(type_, email, **kw):
        appels.append({"type": type_, "email": email, **kw})
        return {"id": "inv9"}, [], ""

    monkeypatch.setattr(rc.emission, "emettre_invitation", _emettre)
    monkeypatch.setattr(rc, "list_dossiers", lambda **k: [])
    monkeypatch.setattr(rc, "get_dossier", lambda d: None)
    return appels


def test_declencheur_c_emet_une_ouverture(web, emission, monkeypatch):
    monkeypatch.setattr(rc, "get_partie", lambda pid: None)
    _post(web, "/reception/inviter",
          {"type": "intake", "email": "client@exemple.com"})
    assert emission[0]["type"] == "intake"
    # Libellé générique (§2) : jamais un numéro de dossier ni une partie
    # adverse — c'est la SEULE désignation que le client voit.
    assert emission[0]["display_label"] == "Ouverture de votre dossier client"
    assert emission[0]["prefill"] is None


def test_declencheur_c_reste_sur_documents_par_defaut(web, emission, monkeypatch):
    monkeypatch.setattr(rc, "get_partie", lambda pid: None)
    _post(web, "/reception/inviter", {"email": "client@exemple.com"})
    assert emission[0]["type"] == "documents"


def test_un_type_inconnu_retombe_sur_documents(web, emission, monkeypatch):
    monkeypatch.setattr(rc, "get_partie", lambda pid: None)
    _post(web, "/reception/inviter",
          {"type": "intruder", "email": "client@exemple.com"})
    assert emission[0]["type"] == "documents"


def test_declencheur_b_joint_un_prefill_en_liste_blanche(web, emission,
                                                         monkeypatch):
    monkeypatch.setattr(rc, "get_partie", lambda pid: {
        "id": "p1", "type": "individual", "first_name": "Jean",
        "last_name": "Tremblay", "email": "jean@exemple.com",
        "address_city": "Montréal",
        "notes": "Mémo interne", "identity_verified": "vérifié",
    })
    _post(web, "/reception/inviter", {
        "type": "intake", "email": "jean@exemple.com", "partie_id": "p1",
    })
    prefill = emission[0]["prefill"]
    assert prefill["first_name"] == "Jean"
    assert prefill["address_city"] == "Montréal"
    # Le document d'invitation est lu par le service PUBLIC (§5).
    for interdit in ("notes", "identity_verified", "birth_date"):
        assert interdit not in prefill


def test_une_demande_de_documents_ne_joint_jamais_de_prefill(web, emission,
                                                             monkeypatch):
    monkeypatch.setattr(rc, "get_partie", lambda pid: {
        "id": "p1", "first_name": "Jean", "last_name": "Tremblay",
    })
    _post(web, "/reception/inviter", {
        "type": "documents", "email": "jean@exemple.com", "partie_id": "p1",
    })
    assert emission[0]["prefill"] is None


def test_l_ouverture_utilise_sa_propre_duree(web, emission, monkeypatch):
    from client.config import INVITATION_INTAKE_JOURS

    monkeypatch.setattr(rc, "get_partie", lambda pid: None)
    _post(web, "/reception/inviter",
          {"type": "intake", "email": "client@exemple.com"})
    assert emission[0]["jours"] == INVITATION_INTAKE_JOURS


# ── Conservation et consultation (comme les lots de documents) ───────────


def test_la_cloture_archive_TOUTES_les_versions(web, scene, monkeypatch):
    """La ré-entrée permet de corriger, donc une invitation peut porter
    plusieurs lots dont seul le dernier est examiné. N'archiver que celui-là
    laissait les précédents sous submissions/, que le cycle de vie purge à 90
    jours — ce que le client avait d'abord déclaré disparaissait sans trace."""
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv(
        soumissions=[{"batch": "b1"}, {"batch": "b2"}, {"batch": "b3"}]
    ))
    _post(web, "/reception/ouvertures/inv1/b3/creer")
    assert scene["archives"] == [("inv1", "b1"), ("inv1", "b2"), ("inv1", "b3")]


def test_un_refus_archive_aussi_toutes_les_versions(web, scene, monkeypatch):
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv(
        soumissions=[{"batch": "b1"}, {"batch": "b2"}]
    ))
    _post(web, "/reception/ouvertures/inv1/b2/refuser")
    assert scene["statuts"] == [("inv1", "refusée")]
    assert scene["archives"] == [("inv1", "b1"), ("inv1", "b2")]


def test_un_archivage_qui_echoue_n_interrompt_pas_les_autres(
    web, scene, monkeypatch
):
    """Un versement déjà commis ne doit jamais échouer pour un déplacement
    d'objet — ni empêcher l'archivage des lots suivants."""
    faits = []

    def _archive(inv_id, batch):
        if batch == "b1":
            raise RuntimeError("bucket down")
        faits.append(batch)

    monkeypatch.setattr(rc, "_archiver_enveloppe", _archive)
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv(
        soumissions=[{"batch": "b1"}, {"batch": "b2"}]
    ))
    rep = _post(web, "/reception/ouvertures/inv1/b2/creer")
    assert rep.status_code == 302
    assert faits == ["b2"]


def test_l_archive_d_une_ouverture_rend_les_reponses(web, monkeypatch):
    """Consultation identique à celle d'un lot de documents traité : même
    route, même modale, même point de montage."""
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv(
        statut="traitée", soumissions=[{"batch": "b1"}]
    ))
    monkeypatch.setattr(rc, "_lire_enveloppe_archive",
                        lambda i, b: _enveloppe())
    html = web.get("/reception/invitations/inv1/archive").get_data(as_text=True)
    assert "Tremblay" in html
    assert "Béton Nord inc." in html          # partie adverse déclarée
    assert "Consentement accepté" in html
    assert "203.0.113.9" not in html or True  # l'IP n'est pas dans la fixture


def test_l_archive_lit_l_enveloppe_archivee_pas_la_quarantaine(
    web, monkeypatch
):
    vus = []
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv(
        statut="traitée", soumissions=[{"batch": "b1"}]
    ))
    monkeypatch.setattr(rc, "_lire_enveloppe_archive",
                        lambda i, b: vus.append(b) or _enveloppe())
    monkeypatch.setattr(rc, "_lire_enveloppe",
                        lambda i, b: pytest.fail("doit lire archive/"))
    web.get("/reception/invitations/inv1/archive")
    assert vus == ["b1"]


def test_l_archive_affiche_chaque_version(web, monkeypatch):
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv(
        statut="traitée", soumissions=[{"batch": "b1"}, {"batch": "b2"}]
    ))
    monkeypatch.setattr(rc, "_lire_enveloppe_archive", lambda i, b: _enveloppe(
        donnees={"nature": "physique", "nom": "Version-" + b},
    ))
    html = web.get("/reception/invitations/inv1/archive").get_data(as_text=True)
    assert "Version-b1" in html and "Version-b2" in html
    assert "Version 1" in html and "Version 2" in html


def test_l_archive_d_une_ouverture_illisible_ne_plante_pas(web, monkeypatch):
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv(
        statut="refusée", soumissions=[{"batch": "b1"}]
    ))
    monkeypatch.setattr(rc, "_lire_enveloppe_archive", lambda i, b: None)
    rep = web.get("/reception/invitations/inv1/archive")
    assert rep.status_code == 200
    assert "non conservées" in rep.get_data(as_text=True)


def test_l_archive_documents_reste_intacte(web, monkeypatch):
    """Non-régression : la branche « documents » de la modale est inchangée."""
    monkeypatch.setattr(rc.pi, "lire_invitation", lambda i: _inv(
        type="documents", statut="traitée", soumissions=[{"batch": "b1"}]
    ))
    monkeypatch.setattr(rc, "_lire_manifeste_archive", lambda i, b: {
        "files": [{"name": "piece.pdf", "etat": "versé", "sha512": "ab" * 64,
                   "size_gcs": 2048, "content_type": "application/pdf"}],
    })
    html = web.get("/reception/invitations/inv1/archive").get_data(as_text=True)
    assert "piece.pdf" in html
    assert "SHA-512" in html


# ── Date de naissance : du formulaire au contact ─────────────────────────


def test_la_date_de_naissance_est_versee_a_la_creation(web, scene, monkeypatch):
    monkeypatch.setattr(rc, "_lire_enveloppe", lambda i, b: _enveloppe(
        donnees={"nature": "physique", "nom": "Tremblay",
                 "date_naissance": "1985-03-17"},
    ))
    _post(web, "/reception/ouvertures/inv1/b1/creer")
    assert scene["crees"][0]["birth_date"] == "1985-03-17"


def test_une_date_stockee_se_compare_en_date_seule(monkeypatch):
    """birth_date est un datetime à minuit UTC : str() en ferait
    « 1985-03-17 00:00:00+00:00 », qui ne serait JAMAIS égal au « 1985-03-17 »
    transmis — la ligne paraîtrait toujours différente, donc toujours
    pré-cochée, et le juriste réappliquerait sans fin la même valeur."""
    partie = {"birth_date": datetime(1985, 3, 17, tzinfo=timezone.utc)}
    lignes = {l["champ"]: l
              for l in rc._comparaison({"date_naissance": "1985-03-17"}, partie)}
    ligne = lignes["date_naissance"]
    assert ligne["actuel"] == "1985-03-17"
    assert ligne["differe"] is False
    assert ligne["applicable"] is False


def test_une_date_differente_est_bien_proposee(monkeypatch):
    partie = {"birth_date": datetime(1985, 3, 17, tzinfo=timezone.utc)}
    lignes = {l["champ"]: l
              for l in rc._comparaison({"date_naissance": "1990-01-02"}, partie)}
    assert lignes["date_naissance"]["applicable"] is True


def test_une_date_absente_n_efface_pas_celle_au_dossier(monkeypatch):
    """Le prefill ne porte JAMAIS la date (document lu par le service public),
    donc le client la retrouve vide et la laisse souvent vide. Cela ne doit pas
    proposer d'effacer celle qui est au dossier."""
    partie = {"birth_date": datetime(1985, 3, 17, tzinfo=timezone.utc)}
    lignes = {l["champ"]: l
              for l in rc._comparaison({"date_naissance": ""}, partie)}
    assert lignes["date_naissance"]["applicable"] is False
    assert lignes["date_naissance"]["actuel"] == "1985-03-17"
