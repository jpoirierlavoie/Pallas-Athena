"""Suppression d'un dossier de classement — la ROUTE et le DIALOGUE.

Le dialogue est le seul garde-fou avant une cascade irréversible : il annonce
le décompte du sous-arbre et offre deux gestes distincts. On l'épingle par un
RENDU RÉEL (la leçon du 2026-08-13 : une épingle de source avait laissé passer
un lien cassé), et la route par son contrat de journalisation — un événement
de suppression par entité, l'invariant maison que l'ancienne version violait
en n'en écrivant qu'un pour le dossier de tête.
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
    import routes.documents as rd

from flask import Flask  # noqa: E402


@pytest.fixture()
def web(monkeypatch):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(
        rd, "get_folder",
        lambda did, fid: {"id": fid, "name": "Pièces", "parent_folder_id": None},
    )
    return client


@pytest.fixture()
def trail(monkeypatch):
    """Capture le registre des suppressions."""
    events: list = []
    monkeypatch.setattr(
        rd, "record_deletion",
        lambda entity_type, entity_id, **kw: events.append(
            {"type": entity_type, "id": entity_id, **kw}
        ),
    )
    return events


def _rapport(documents=(), folders=(), moved=0) -> dict:
    return {
        "documents": [dict(d) for d in documents],
        "folders": [dict(f) for f in folders],
        "moved": moved,
    }


# ── Le contrat de journalisation ───────────────────────────────────────────


def test_un_evenement_par_entite_supprimee(web, trail, monkeypatch):
    """L'invariant maison (14 sites) : un record_deletion PAR entité. La
    version précédente n'en écrivait qu'un, pour le dossier de tête — les
    sous-dossiers détruits ne laissaient aucune trace, et aucun document
    n'en laissait puisque aucun n'était supprimé."""
    rapport = _rapport(
        documents=[
            {"id": "a", "display_name": "Requête", "category": "procédure"},
            {"id": "b", "display_name": "Annexe", "category": "pièce"},
        ],
        folders=[{"id": "f1", "name": "Pièces"}, {"id": "f2", "name": "Annexes"}],
    )
    monkeypatch.setattr(rd, "delete_folder", lambda *a, **k: (True, "", rapport))

    reponse = web.post("/documents/folders/f1/delete",
                       data={"dossier_id": "d1", "contents": "delete"})
    assert reponse.status_code == 302
    assert [e["type"] for e in trail] == ["document", "document", "folder", "folder"]
    assert {e["id"] for e in trail} == {"a", "b", "f1", "f2"}
    docs = [e for e in trail if e["type"] == "document"]
    assert docs[0]["title"] == "Requête" and docs[0]["status"] == "procédure"
    assert all(e["dossier_id"] == "d1" for e in trail)


def test_le_deplacement_ne_journalise_que_les_dossiers(web, trail, monkeypatch):
    rapport = _rapport(folders=[{"id": "f1", "name": "Pièces"}], moved=3)
    monkeypatch.setattr(rd, "delete_folder", lambda *a, **k: (True, "", rapport))

    web.post("/documents/folders/f1/delete",
             data={"dossier_id": "d1", "contents": "move"})
    assert [e["type"] for e in trail] == ["folder"]
    assert trail[0]["status"] == ""      # « contenu supprimé » seulement si oui


def test_un_echec_qui_n_a_rien_detruit_ne_journalise_rien(web, trail, monkeypatch):
    monkeypatch.setattr(
        rd, "delete_folder",
        lambda *a, **k: (False, "Le dossier a été conservé — réessayez.", _rapport()),
    )
    reponse = web.post("/documents/folders/f1/delete",
                       data={"dossier_id": "d1", "contents": "delete"})
    assert trail == []
    # La branche sans JS rebondit désormais avec ?erreur= au lieu de laisser
    # tomber l'erreur en silence.
    assert reponse.status_code == 302
    assert "erreur=" in reponse.headers["Location"]


def test_un_echec_PARTIEL_journalise_ce_qui_est_deja_parti(web, trail, monkeypatch):
    """Le constat central de la revue du 2026-08-14. Deux des retours d'échec
    de delete_folder ne sont PAS atomiques : la suppression d'octets qui casse
    au 3e fichier laisse les deux premiers définitivement partis (blob GCS ET
    ligne Firestore) et les porte dans le compte-rendu. Journaliser sous
    « if success: » jetait ce rapport, et list_deletions — dont la fonction
    ENTIÈRE est de répondre « qu'est-ce qui a disparu ? » — répondait que rien
    n'avait disparu. Le trou était permanent : une reprise ne peut pas
    ré-énumérer des lignes détruites."""
    rapport = _rapport(documents=[
        {"id": "a", "display_name": "Requête", "category": "procédure"},
        {"id": "b", "display_name": "Annexe", "category": "pièce"},
    ])
    monkeypatch.setattr(
        rd, "delete_folder",
        lambda *a, **k: (
            False,
            "2 fichier(s) supprimé(s), puis : Erreur. Le dossier a été conservé.",
            rapport,
        ),
    )
    reponse = web.post("/documents/folders/f1/delete",
                       data={"dossier_id": "d1", "contents": "delete"})

    assert [e["id"] for e in trail] == ["a", "b"]
    assert all(e["type"] == "document" for e in trail)
    assert trail[0]["title"] == "Requête"
    # Le dossier de tête, lui, tient toujours : rien à journaliser pour lui.
    assert not [e for e in trail if e["type"] == "folder"]
    # Et l'écran dit l'échec, pas un succès.
    from urllib.parse import unquote_plus

    cible = unquote_plus(reponse.headers["Location"])
    assert "erreur=" in reponse.headers["Location"]
    assert "2 fichier(s) supprimé(s)" in cible
    assert "message=" not in reponse.headers["Location"]


def test_l_echec_htmx_rebondit_au_lieu_de_rendre_un_422(web, trail, monkeypatch):
    """htmx 2.0.4 n'échange QUE les 2xx (règle documentée à
    doc_templates.py:88). Le fragment 422 d'origine ne se rendait donc
    jamais : le refus de plafond — la seule phrase qui dise quoi faire — et
    l'aveu d'une destruction partielle mouraient à l'écran, bouton mort,
    rien qui bouge, et l'utilisateur re-cliquait.

    L'échec emprunte désormais la voie du succès : redirection que le XHR
    suit, si bien que document_list (l. 182) rend le fragment _browser.html
    avec ?erreur= dans sa bannière — le message paraît ET la liste reste."""
    monkeypatch.setattr(
        rd, "delete_folder", lambda *a, **k: (False, "Trop de fichiers.", _rapport()),
    )
    reponse = web.post("/documents/folders/f1/delete",
                       data={"dossier_id": "d1", "contents": "delete"},
                       headers={"HX-Request": "true"})
    from urllib.parse import unquote_plus

    assert reponse.status_code == 302
    assert "Trop de fichiers." in unquote_plus(reponse.headers["Location"])
    # Et jamais un 4xx : htmx le laisserait tomber sans rien échanger.
    assert reponse.status_code < 400


# ── Le mode transmis au modèle ─────────────────────────────────────────────


@pytest.mark.parametrize("envoye", ["delete", "move", "", "true", "recursive"])
def test_le_mode_du_formulaire_est_transmis_tel_quel(web, trail, monkeypatch, envoye):
    """La route ne réinterprète pas : le MODÈLE est l'unique arbitre, et il
    retombe sur « move » pour toute valeur inconnue."""
    vus: list = []

    # Élargi délibérément (lot 2A, T2) : le modèle reçoit aussi le décompte
    # que le dialogue a annoncé — absent ici (page d'avant T2), donc rien.
    def faux_delete(dossier_id, folder_id, *, contents="move",
                    expected_documents=None, expected_folders=None):
        vus.append((contents, expected_documents, expected_folders))
        return True, "", _rapport(folders=[{"id": folder_id, "name": "X"}])

    monkeypatch.setattr(rd, "delete_folder", faux_delete)
    web.post("/documents/folders/f1/delete",
             data={"dossier_id": "d1", "contents": envoye})
    assert vus == [(envoye, None, None)]


# ── Le décompte annoncé (lot 2A, T2) ───────────────────────────────────────


def test_le_decompte_annonce_est_transmis_au_modele(web, trail, monkeypatch):
    """Le connecteur pourra déplacer fichiers et dossiers : le dialogue
    RENVOIE ce qu'il a annoncé, et c'est le MODÈLE qui compare au sous-arbre
    réel (tests/test_folders.py::test_a_delete_on_a_stale_count_touches_nothing)."""
    vus: list = []

    def faux_delete(dossier_id, folder_id, **kwargs):
        vus.append(kwargs)
        return True, "", _rapport(folders=[{"id": folder_id, "name": "X"}])

    monkeypatch.setattr(rd, "delete_folder", faux_delete)
    web.post("/documents/folders/f1/delete", data={
        "dossier_id": "d1", "contents": "delete",
        "expected_documents": "23", "expected_folders": "4",
    })
    assert vus == [{"contents": "delete", "expected_documents": 23,
                    "expected_folders": 4}]


@pytest.mark.parametrize("champ, valeur", [
    ("expected_documents", "abc"), ("expected_documents", "-1"),
    ("expected_folders", "²"), ("expected_folders", "1" * 12),
])
def test_un_decompte_illisible_refuse_sans_rien_toucher(
    web, trail, monkeypatch, champ, valeur,
):
    """Un POST fabriqué : refus en 2xx (la bannière — htmx n'échange que les
    2xx), et le modèle n'est même pas appelé."""
    monkeypatch.setattr(rd, "delete_folder",
                        lambda *a, **k: pytest.fail("suppression appelée"))
    reponse = web.post("/documents/folders/f1/delete",
                       data={"dossier_id": "d1", "contents": "delete",
                             champ: valeur},
                       headers={"HX-Request": "true"})
    from urllib.parse import unquote_plus

    assert reponse.status_code == 302
    assert "Rien n'a été supprimé" in unquote_plus(reponse.headers["Location"])
    assert trail == []


def test_le_refus_du_modele_sur_un_decompte_perime_parait_dans_la_banniere(
    web, trail, monkeypatch,
):
    from models import folder as folder_model

    message = folder_model.SUBTREE_CHANGED.format(
        documents=2, ds="s", folders=0, fs="")
    monkeypatch.setattr(rd, "delete_folder",
                        lambda *a, **k: (False, message, _rapport()))
    reponse = web.post("/documents/folders/f1/delete", data={
        "dossier_id": "d1", "contents": "delete",
        "expected_documents": "1", "expected_folders": "0",
    }, headers={"HX-Request": "true"})
    from urllib.parse import unquote_plus

    assert reponse.status_code == 302
    assert "a changé depuis l'affichage" in unquote_plus(reponse.headers["Location"])
    assert trail == []


def test_le_message_annonce_ce_qui_a_disparu(web, trail, monkeypatch):
    rapport = _rapport(
        documents=[{"id": "a", "display_name": "R", "category": "autre"}],
        folders=[{"id": "f1", "name": "P"}, {"id": "f2", "name": "A"}],
    )
    monkeypatch.setattr(rd, "delete_folder", lambda *a, **k: (True, "", rapport))
    reponse = web.post("/documents/folders/f1/delete",
                       data={"dossier_id": "d1", "contents": "delete"})
    from urllib.parse import unquote_plus

    cible = unquote_plus(reponse.headers["Location"])
    assert "2 dossiers et 1 fichier supprimés" in cible


def test_le_message_du_deplacement_dit_le_nombre_deplace(web, trail, monkeypatch):
    rapport = _rapport(folders=[{"id": "f1", "name": "P"}], moved=3)
    monkeypatch.setattr(rd, "delete_folder", lambda *a, **k: (True, "", rapport))
    reponse = web.post("/documents/folders/f1/delete",
                       data={"dossier_id": "d1", "contents": "move"})
    from urllib.parse import unquote_plus

    cible = unquote_plus(reponse.headers["Location"])
    assert "3 fichiers déplacés vers le dossier parent" in cible


# ── Le dialogue, rendu RÉELLEMENT ──────────────────────────────────────────


@pytest.fixture()
def rendu(monkeypatch):
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.register_blueprint(rd.documents_bp)
    return app


def _browser(rendu, folders) -> str:
    with rendu.test_request_context("/documents/?dossier_id=d1"):
        from flask import render_template

        return render_template(
            "documents/_browser.html", documents=[], folders=folders,
            breadcrumb=[], dossier_id="d1", folder_id=None,
            category_filter="", search="", sort_by="created_at",
            category_labels={}, valid_categories=(), pagination=None,
            erreur="", message="",
        )


def _dossier_ligne(**over) -> dict:
    base = {"id": "f1", "name": "Pièces", "_item_count": 2,
            "_subtree_documents": 23, "_subtree_folders": 4}
    base.update(over)
    return base


def test_le_dialogue_offre_les_deux_gestes_avec_le_decompte(rendu):
    html = _browser(rendu, [_dossier_ligne()])
    assert "23 fichier" in html and "4 sous-dossier" in html
    assert "Déplacer les fichiers" in html
    assert "Tout supprimer" in html
    assert 'name="contents" value="move"' in html
    assert 'name="contents" value="delete"' in html
    # Le champ « recursive » de l'ancien contrat a disparu.
    assert 'name="recursive"' not in html
    # Deux formulaires DISTINCTS : rien ne dépend de l'inclusion du bouton
    # soumetteur par HTMX, et le repli sans JS reste exact.
    assert html.count('action="/documents/folders/f1/delete"') == 2


def test_les_deux_formulaires_renvoient_le_decompte_annonce(rendu):
    html = _browser(rendu, [_dossier_ligne()])
    assert html.count('name="expected_documents" value="23"') == 2
    assert html.count('name="expected_folders" value="4"') == 2


def test_un_dossier_vide_renvoie_un_decompte_nul(rendu):
    """Un décompte en échec pose des zéros : le formulaire les renvoie, et
    le modèle refuse si le dossier n'est pas vraiment vide — le dialogue ne
    peut plus confirmer ce qu'il n'a pas montré."""
    html = _browser(rendu, [{"id": "f1", "name": "Pièces", "_item_count": 0}])
    assert 'name="expected_documents" value="0"' in html
    assert 'name="expected_folders" value="0"' in html


def test_le_formulaire_de_renommage_porte_la_version_affichee(rendu):
    html = _browser(rendu, [_dossier_ligne(etag="e-42")])
    assert 'name="expected_etag" value="e-42"' in html
    # Un dossier d'avant T2 (sans etag) renvoie la chaîne vide, qui désigne
    # exactement cette version-là (models/concurrency).
    html = _browser(rendu, [_dossier_ligne()])
    assert 'name="expected_etag" value=""' in html


def test_un_dossier_systeme_ne_propose_pas_le_renommage(rendu):
    html = _browser(rendu, [_dossier_ligne(_system=True, name="Projets")])
    assert "Renommer" not in html
    assert "dossier de l'application" in html
    # Il reste téléchargeable et supprimable — la décision du juriste.
    assert "Télécharger (zip)" in html and "Supprimer" in html
    html = _browser(rendu, [_dossier_ligne()])
    assert "Renommer" in html


def test_un_dossier_vide_n_offre_qu_un_geste(rendu):
    html = _browser(rendu, [_dossier_ligne(_subtree_documents=0,
                                           _subtree_folders=0,
                                           _item_count=0)])
    assert "Ce dossier est vide." in html
    assert "Tout supprimer" not in html
    assert "Déplacer les fichiers" not in html
    assert 'name="contents" value="delete"' not in html
    assert html.count('action="/documents/folders/f1/delete"') == 1


def test_un_dossier_de_sous_dossiers_vides_ne_menace_aucun_fichier(rendu):
    html = _browser(rendu, [_dossier_ligne(_subtree_documents=0,
                                           _subtree_folders=2)])
    assert "2 sous-dossiers vides" in html
    assert 'name="contents" value="delete"' not in html


def test_le_decompte_absent_ne_propose_jamais_la_suppression(rendu):
    """Si subtree_index a échoué, la route pose des zéros : le dialogue doit
    alors se comporter comme un dossier vide — jamais offrir « tout
    supprimer » sur un décompte qu'il ne connaît pas."""
    html = _browser(rendu, [{"id": "f1", "name": "Pièces", "_item_count": 0}])
    assert 'name="contents" value="delete"' not in html


def test_la_banniere_de_message_se_rend(rendu):
    with rendu.test_request_context("/documents/?dossier_id=d1"):
        from flask import render_template

        html = render_template(
            "documents/_browser.html", documents=[], folders=[],
            breadcrumb=[], dossier_id="d1", folder_id=None,
            category_filter="", search="", sort_by="created_at",
            category_labels={}, valid_categories=(), pagination=None,
            erreur="", message="2 dossiers et 23 fichiers supprimés",
        )
    assert "2 dossiers et 23 fichiers supprimés" in html


# ── Renommer, sur le vrai modèle (lot 2A, T2) ──────────────────────────────


@pytest.fixture()
def web_reel(monkeypatch):
    """La route ET le modèle réels, au-dessus du faux Firestore partagé."""
    from models import folder as folder_model
    from tests._fake_firestore import install

    store = install(monkeypatch, folder_model)
    store.seed("dossiers/d1", {"id": "d1"})
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client, store


def _erreur(reponse) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(reponse.headers["Location"]).query).get("erreur", [""])[0]


def test_un_renommage_sur_une_page_perimee_est_refuse_dans_la_banniere(web_reel):
    from models import concurrency

    client, store = web_reel
    store.seed("folders/f1", {"id": "f1", "dossier_id": "d1", "name": "Pièces",
                              "parent_folder_id": None, "etag": "neuf"})
    reponse = client.post("/documents/folders/f1/rename", data={
        "dossier_id": "d1", "new_name": "Autre", "expected_etag": "ancien",
    }, headers={"HX-Request": "true"})
    assert reponse.status_code == 302
    assert _erreur(reponse) == concurrency.STALE_ETAG_ERROR
    assert store.peek("folders/f1")["name"] == "Pièces"

    # La bonne version passe, et le renommage est une écriture partielle.
    reponse = client.post("/documents/folders/f1/rename", data={
        "dossier_id": "d1", "new_name": "Autre", "expected_etag": "neuf",
    })
    assert _erreur(reponse) == ""
    assert store.peek("folders/f1")["name"] == "Autre"


def test_renommer_un_dossier_systeme_est_refuse_meme_par_un_post_fabrique(web_reel):
    """Le menu ne propose pas « Renommer » sur « Projets », mais une page
    périmée ou un POST fabriqué le peut : c'est le MODÈLE qui refuse."""
    client, store = web_reel
    store.seed("folders/p", {"id": "p", "dossier_id": "d1", "name": "Projets",
                             "parent_folder_id": None, "system_role": "projets"})
    reponse = client.post("/documents/folders/p/rename",
                          data={"dossier_id": "d1", "new_name": "Brouillons"})
    assert "dossier système" in _erreur(reponse)
    assert store.peek("folders/p")["name"] == "Projets"


def test_le_navigateur_marque_le_dossier_systeme_herite(web_reel):
    """Un « Projets » créé par nom avant T2 (sans rôle) est reconnu comme LE
    dossier système — le plus ancien de la racine —, un sous-dossier
    homonyme ne l'est pas."""
    client, store = web_reel
    base = {"dossier_id": "d1", "parent_folder_id": None}
    store.seed("folders/leg", {"id": "leg", "name": "Projets", **base})
    store.seed("folders/pc", {"id": "pc", "name": "Pièces", **base})
    folders = rd.list_folders("d1", parent_folder_id=None)
    rd._attach_folder_counts(folders, "d1")
    flags = {f["id"]: f["_system"] for f in folders}
    assert flags == {"leg": True, "pc": False}
