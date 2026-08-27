"""models/chat_charter.py — la charte éditable et versionnée (Phase N).

Le singleton, la création implicite transactionnelle, le plancher de
version qui protège le sens de « 1 », et les refus qui empêchent une
charte vide de faire répondre 400 à toutes les conversations d'un coup.
Même faux Firestore que la famille versionnée-append-only.
"""

import os
import sys
import uuid
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

# Le stub google.cloud.firestore est installé par test_chat_skill à l'import.
from tests.test_chat_skill import _FakeDB, _FakeFirestore  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import models.chat_charter as cc  # noqa: E402
    import models.chat_reference_files as reference_files  # noqa: E402


@pytest.fixture()
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(cc, "db", _FakeDB(data))
    monkeypatch.setattr(cc, "firestore", _FakeFirestore)
    return data


_CORPS = (
    "RÈGLES DE SORTIE\n\n"
    "Tu réponds en français, en markdown, et en markdown uniquement.\n\n"
    "DISCIPLINE D'ÉCRITURE\n\n"
    "Avant un geste conséquent, pose la question et attends la réponse.\n"
    "Propose d'abord par dry_run: true, puis commets sur instruction.\n"
) * 2


def _versions(store):
    return store.get(f"chat_charter/{cc.DOC_ID}/versions", {})


def _contents(store):
    return store.get(f"chat_charter/{cc.DOC_ID}/fichiers", {})


# ── Le singleton et la numérotation ─────────────────────────────────────────

def test_firestore_starts_at_two_because_one_is_the_source_text(store):
    """« 1 » désigne pour toujours les octets de chat.charter.BASE_CHARTER.

    Tous les tours enregistrés avant ce lot le portent. Faire repartir
    Firestore à 1 ferait mentir le registre sur tout son passé.
    """
    doc, errors = cc.revise_charter(body=_CORPS)
    assert errors == []
    assert doc["current_version"] == cc.FIRST_FIRESTORE_VERSION == 2
    assert "000002" in _versions(store)
    assert "000001" not in _versions(store)


def test_a_corrupted_head_can_never_mint_version_one(store):
    """Le plancher est un `max`, pas de la décoration."""
    cc.revise_charter(body=_CORPS)
    store["chat_charter"][cc.DOC_ID]["current_version"] = 0
    doc, errors = cc.revise_charter(body=_CORPS + "suite")
    assert errors == []
    assert doc["current_version"] == 2


def test_only_one_document_id_is_ever_addressed(store):
    for n in range(3):
        cc.revise_charter(body=_CORPS + str(n))
    assert list(store["chat_charter"]) == [cc.DOC_ID]


def test_the_charter_id_can_never_collide_with_a_skill_id():
    """Les compétences frappent des uuid4 ; « charte » n'en est pas un."""
    with pytest.raises(ValueError):
        uuid.UUID(cc.DOC_ID)


# ── La création implicite, transactionnelle ─────────────────────────────────

def test_revise_creates_implicitly_and_appends_thereafter(store):
    """UN seul verbe. Un `create_charter` séparé exigerait une première
    écriture hors transaction, et deux onglets ouverts sur un formulaire
    vierge frapperaient tous deux la v2 : le second `set` écraserait un
    document de version write-once en silence, après quoi tout tour
    estampillé « 2 » désignerait un texte que personne n'a jamais vu."""
    premier, _ = cc.revise_charter(body=_CORPS)
    assert premier["current_version"] == 2
    assert premier["created_at"] is not None

    second, _ = cc.revise_charter(body=_CORPS + "\n\nUne règle de plus.")
    assert second["current_version"] == 3
    # La v2 est intacte, et created_at n'a pas bougé.
    assert _versions(store)["000002"]["body"] == premier["body"]
    assert second["created_at"] == premier["created_at"]
    assert second["updated_at"] != premier["updated_at"]
    assert second["etag"] != premier["etag"]


def test_the_head_carries_body_addendum_and_manifest(store):
    doc, errors = cc.revise_charter(
        body=_CORPS,
        addendum="EXÉCUTION PLANIFIÉE\n\nN'attends aucune réponse.",
        files=[{"name": "Grille", "description": "Aide-mémoire.",
                "content": "Une ligne de référence."}],
    )
    assert errors == []
    assert "EXÉCUTION PLANIFIÉE" in doc["addendum"]
    assert [f["name"] for f in doc["files"]] == ["Grille"]
    # Le manifeste ne porte JAMAIS le contenu (la garde du 1 Mio/document).
    assert "content" not in doc["files"][0]
    assert len(_contents(store)) == 1
    # Et le document de version est auto-suffisant.
    version = _versions(store)["000002"]
    assert version["addendum"] == doc["addendum"]
    assert version["files"] == doc["files"]


def test_files_none_keeps_the_manifest_a_list_replaces_it(store):
    cc.revise_charter(
        body=_CORPS,
        files=[{"name": "Grille", "description": "", "content": "un"}],
    )
    garde, _ = cc.revise_charter(body=_CORPS + "a")
    assert [f["name"] for f in garde["files"]] == ["Grille"]
    vide, _ = cc.revise_charter(body=_CORPS + "b", files=[])
    assert vide["files"] == []
    # Append-only : la version qui le référençait garde son manifeste, et
    # le document de contenu n'est jamais supprimé.
    assert _versions(store)["000002"]["files"][0]["name"] == "Grille"
    assert len(_contents(store)) == 1


# ── Les refus : une charte vide briquerait TOUT ────────────────────────────

def test_an_over_long_body_is_refused_never_truncated(store):
    """`sanitize` tronque en silence : la constitution perdrait sa
    dernière règle sans un mot. La longueur se mesure donc sur la valeur
    BRUTE, avant tout nettoyage."""
    trop = "é" * (cc.BODY_MAX_LENGTH + 1)
    doc, errors = cc.revise_charter(body=trop)
    assert doc is None
    assert any("dépasse" in e for e in errors)
    assert store == {}


def test_an_empty_or_too_short_body_is_refused(store):
    for corps in ("", "   ", "Trop court."):
        doc, errors = cc.revise_charter(body=corps)
        assert doc is None, corps
        assert errors, corps
    assert store == {}


def test_an_over_long_addendum_is_refused(store):
    doc, errors = cc.revise_charter(
        body=_CORPS, addendum="x" * (cc.ADDENDUM_MAX_LENGTH + 1)
    )
    assert doc is None
    assert any("addendum" in e.lower() for e in errors)


def test_the_body_is_sanitized_but_a_file_stays_verbatim(store):
    """L'asymétrie est le sujet : le corps est rendu en markdown|safe (donc
    il DOIT être nettoyé — sans quoi c'est un XSS stocké), le contenu d'un
    fichier est rendu en <pre> sous autoescape et doit rester intact."""
    doc, errors = cc.revise_charter(
        body=_CORPS + "<script>alert(1)</script>",
        files=[{"name": "Modèle", "description": "",
                "content": "Voici <placeholder> et <xml/>."}],
    )
    assert errors == []
    assert "<script>" not in doc["body"]
    contenu = list(_contents(store).values())[0]["content"]
    assert contenu == "Voici <placeholder> et <xml/>."


# ── La lecture : trois états, jamais deux ──────────────────────────────────

def test_get_head_is_a_tristate(store, monkeypatch):
    assert cc.get_head() == (None, "absent")
    cc.revise_charter(body=_CORPS)
    doc, statut = cc.get_head()
    assert statut == "ok" and doc["current_version"] == 2

    def _explose(*a, **kw):
        raise RuntimeError("Firestore indisponible")

    monkeypatch.setattr(cc.db, "collection", _explose)
    assert cc.get_head() == (None, "erreur")


def test_a_blank_body_head_reads_as_erreur_not_ok(store):
    """`charter.system_blocks` construit le bloc 0 SANS garde : un bloc
    texte vide fait répondre 400 à Vertex sur toutes les conversations à
    la fois. `_clean` refuse déjà d'en écrire un ; une écriture hors
    application le pourrait, d'où le refus AUSSI à la lecture."""
    cc.revise_charter(body=_CORPS)
    store["chat_charter"][cc.DOC_ID]["body"] = "   "
    assert cc.get_head() == (None, "erreur")


def test_get_version_one_returns_none_the_source_is_the_callers_business():
    """Ce module ignore `chat/` — le sens des dépendances est
    `chat/` → `models/`, jamais l'inverse. Et le nombre lui-même ne
    voyage pas : `is_source_version` est ce que l'appelant interroge."""
    assert cc.is_source_version(1) is True
    assert cc.is_source_version(2) is False
    assert cc.is_source_version(None) is True
    assert cc.is_source_version("charte") is True
    assert cc.get_version(1) is None


def test_get_version_reads_the_pinned_doc(store):
    cc.revise_charter(body=_CORPS)
    cc.revise_charter(body=_CORPS + "\n\nAjout de la v3.")
    v2 = cc.get_version(2)
    assert v2 is not None and "Ajout de la v3." not in v2["body"]
    assert cc.get_version(99) is None


def test_get_version_file_resolves_at_the_pinned_version(store):
    cc.revise_charter(
        body=_CORPS,
        files=[{"name": "Grille", "description": "", "content": "v2"}],
    )
    cc.revise_charter(
        body=_CORPS,
        files=[{"name": "Grille", "description": "", "content": "v3"}],
    )
    assert cc.get_version_file(2, "Grille") == ("v2", None)
    assert cc.get_version_file(3, "grille") == ("v3", None)  # casse pliée
    contenu, motif = cc.get_version_file(3, "Inconnue")
    assert contenu is None and "Grille" in motif
    # La version source n'a pas de fichiers, et le dit en français.
    contenu, motif = cc.get_version_file(1, "Grille")
    assert contenu is None and "aucun fichier" in motif


# ── La doctrine ────────────────────────────────────────────────────────────

def test_neither_deletion_nor_deactivation_exists():
    """Il y a toujours exactement une charte : la désactiver n'aurait pas
    de sens, et rien ne se supprime dans le clavardage."""
    for attr in dir(cc):
        assert not attr.startswith("delete"), attr
        assert not attr.startswith("set_active"), attr
    with open(cc.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "def delete" not in source
    assert ".delete(" not in source
    assert "def set_active" not in source


def test_the_module_never_imports_chat():
    """Le texte source reste l'affaire de l'appelant."""
    with open(cc.__file__, encoding="utf-8") as fh:
        source = fh.read()
    for ligne in source.splitlines():
        depouille = ligne.strip()
        if depouille.startswith(("import ", "from ")):
            assert not depouille.startswith(("import chat", "from chat")), ligne
