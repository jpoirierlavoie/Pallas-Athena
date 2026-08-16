"""Route-layer tests for the per-party roles + avocat rework (July 2026).

The hidden JSON fields round-trip through the browser, so the parser is the
security boundary: an explicit whitelist, junk roles dropped, avocat pair
coerced — never **entry.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

import pytest

with mock.patch("google.cloud.firestore.Client"):
    import models.dossier as dossier_model
    from routes.dossiers import _parse_parties_json


def test_parse_accepts_the_full_shape():
    raw = ('[{"id": "p1", "name": "Jean", "roles": ["défendeur", '
           '"demandeur reconventionnel"], "avocat_id": "av1", '
           '"avocat_name": "Roy"}]')
    assert _parse_parties_json(raw) == [{
        "id": "p1", "name": "Jean",
        "roles": ["défendeur", "demandeur reconventionnel"],
        "avocat_id": "av1", "avocat_name": "Roy",
    }]


def test_parse_accepts_a_legacy_bare_entry():
    """Old Alpine state (or a stale open form) posts {id, name} only."""
    assert _parse_parties_json('[{"id": "p1", "name": "Jean"}]') == [{
        "id": "p1", "name": "Jean", "roles": [],
        "avocat_id": "", "avocat_name": "",
    }]


def test_parse_drops_junk_roles_and_foreign_keys():
    """Roles outside the vocabulary are dropped (only a crafted POST can
    produce them), and unknown keys never pass through."""
    raw = ('[{"id": "p1", "name": "J", "roles": ["demandeur", "capitaine", 7],'
           ' "avocat_id": null, "sneaky": "x"}]')
    parsed = _parse_parties_json(raw)
    assert parsed == [{
        "id": "p1", "name": "J", "roles": ["demandeur"],
        "avocat_id": "", "avocat_name": "",
    }]
    assert "sneaky" not in parsed[0]


def test_parse_tolerates_a_non_list_roles_value():
    raw = '[{"id": "p1", "name": "J", "roles": "demandeur"}]'
    assert _parse_parties_json(raw)[0]["roles"] == []


# ── get_dossier_by_file_number ─────────────────────────────────────────────
# Une requête à clé, et qui LÈVE : le nouvel appelant (la reprise historique)
# demande « ce numéro existe-t-il déjà ? » juste avant d'en créer un, et il ne
# peut rien supprimer si la réponse est fausse.


class _Snap:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Query:
    def __init__(self, rows, fail=False):
        self._rows = rows
        self._fail = fail
        self._value = None

    def where(self, filter=None):
        self._value = filter.value
        return self

    def limit(self, n):
        return self

    def stream(self):
        if self._fail:
            raise RuntimeError("firestore unavailable")
        return [_Snap(r) for r in self._rows if r.get("file_number") == self._value][:1]


class _DB:
    def __init__(self, rows, fail=False):
        self._rows = rows
        self._fail = fail

    def collection(self, name):
        return _Query(self._rows, self._fail)


class _FF:
    def __init__(self, field_path=None, op_string=None, value=None, **_k):
        self.field_path = field_path
        self.op_string = op_string
        self.value = value


@pytest.fixture
def _fs(monkeypatch):
    def _install(rows, fail=False):
        monkeypatch.setattr(dossier_model, "db", _DB(rows, fail))
        monkeypatch.setattr(dossier_model, "FieldFilter", _FF)

    return _install


def test_get_dossier_by_file_number_leve_sur_echec_de_requete(_fs):
    """Fail CLOSED. get_dossier, lui, avale et rend None — l'asymétrie est
    voulue : ici « introuvable » sert à décider de CRÉER."""
    _fs([], fail=True)
    with pytest.raises(RuntimeError):
        dossier_model.get_dossier_by_file_number("2014-007")


def test_get_dossier_by_file_number_rend_la_forme_migree(_fs):
    """La même forme que get_dossier — migrations appliquées, champs retirés
    purgés — pour qu'un appelant n'ait jamais deux formes à gérer."""
    _fs([{
        "id": "d-old",
        "file_number": "2014-007",
        "title": "Succession",
        # Champs retirés (juillet 2026) : _strip_removed_fields les purge.
        "notes": "vieille note",
        "matter_type": "recouvrement",
        # Forme héritée à un seul client : _migrate_parties la remonte.
        "client_id": "p1",
    }])
    doc = dossier_model.get_dossier_by_file_number("2014-007")
    assert doc["id"] == "d-old"
    assert "notes" not in doc and "matter_type" not in doc
    assert doc["clients"] and doc["clients"][0]["id"] == "p1"
    # _migrate_domaine tourne AVANT la purge, donc le legacy matter_type a
    # bien été replié plutôt que perdu.
    assert doc["domaine"] == "REC"


def test_get_dossier_by_file_number_sans_correspondance_rend_none(_fs):
    _fs([{"id": "d1", "file_number": "2026-001", "title": "X"}])
    assert dossier_model.get_dossier_by_file_number("2014-007") is None


def test_get_dossier_by_file_number_ignore_un_numero_vide(_fs):
    """Pas de requête du tout — une chaîne vide ne doit pas balayer la
    collection ni ramener un dossier au hasard."""
    _fs([{"id": "d1", "file_number": "", "title": "X"}])
    assert dossier_model.get_dossier_by_file_number("") is None
    assert dossier_model.get_dossier_by_file_number("   ") is None


def test_get_dossier_by_file_number_rogne_les_blancs(_fs):
    _fs([{"id": "d1", "file_number": "2026-001", "title": "X"}])
    assert dossier_model.get_dossier_by_file_number("  2026-001 ")["id"] == "d1"


def test_une_partie_sans_id_leve_un_KeyError_documente():
    """_rebuild_party_mirrors indice en BRUT (c["id"]), donc une entrée sans
    « id » lève un KeyError NON RATTRAPÉ dans create_dossier/update_dossier —
    un 500, pas une erreur de validation, et _validate ne vérifie pas la
    présence de la clé. C'est la raison d'être de la résolution préalable
    côté connecteur : ce test épingle le comportement pour qu'elle ne
    disparaisse jamais comme « garde superflue »."""
    data = {"clients": [{"name": "Sans identifiant"}], "opposing_parties": []}
    with pytest.raises(KeyError):
        dossier_model._rebuild_party_mirrors(data)
