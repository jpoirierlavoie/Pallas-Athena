"""Le profil du cabinet — `settings/cabinet` et son unique dérivation.

Ce fichier épingle les propriétés dont dépend le reste du lot, et deux
d'entre elles valent d'être nommées ici parce qu'elles échouent EN SILENCE :

* `_read_raw` refuse un instantané qui n'est pas un mapping. Le harnais de
  `test_taches_portail.py` remplace `firestore.Client` par un `MagicMock`,
  dont l'instantané a un `.exists` VRAI et rend un `MagicMock` de
  `.to_dict()` : sans la garde, « <MagicMock …> » s'interpolerait dans un
  courriel client et la plupart des assertions passeraient quand même.
* `_composer_cabinet` n'est PAS évalué à l'import. `main.py` importe
  `routes/taches_portail` inconditionnellement, donc une lecture Firestore
  au niveau module mettrait un aller-retour réseau dans `create_app()` et
  GÈLERAIT le profil pour toute la vie du worker.

L'ancrage des `FIRM_*` est une fixture autouse LOCALE à ce fichier. Pas de
`tests/conftest.py` : une dizaine de fichiers lisent le profil du cabinet et
dépendent du chemin à blanc (le repli codé en dur de `taches_portail`) —
geler les valeurs pour tous d'un coup retournerait des assertions.
"""

import io
import os
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from config import Config
    from models import settings as S
    from utils.cabinet import CABINET_KEYS, cabinet_dict

_ROOT = Path(__file__).resolve().parents[1]

# Les valeurs de production (app.yaml), pour prouver que le chemin de
# semence reproduit ce que l'application imprimait déjà.
_PROD = {
    "FIRM_NAME": "Me Jason Poirier Lavoie",
    "FIRM_STREET": "4970, chemin de la Côte-des-Neiges",
    "FIRM_UNIT": "suite 9",
    "FIRM_CITY": "Montréal",
    "FIRM_PROVINCE": "QC",          # non défini en production → défaut du code
    "FIRM_POSTAL_CODE": "H3V 1A4",
    "FIRM_PHONE": "(514) 737-2525",
    "FIRM_FAX": "(514) 737-6565",
    "FIRM_EMAIL": "reception@poirierlavoie.ca",
    "GST_NUMBER": "",
    "QST_NUMBER": "",
}


# ── Faux Firestore ───────────────────────────────────────────────────────
# Volontairement plus strict que le MagicMock du dépôt : `exists` est un
# booléen VRAI et `to_dict()` rend un dict ou None, comme le vrai magasin.

class _FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakeDoc:
    def __init__(self, store: dict, key: str):
        self._store, self._key = store, key

    def get(self):
        return _FakeSnap(self._store.get(self._key))

    def set(self, data):
        self._store[self._key] = dict(data)


class _FakeCollection:
    def __init__(self, store: dict, name: str):
        self._store, self._name = store, name

    def document(self, doc_id: str):
        return _FakeDoc(self._store, f"{self._name}/{doc_id}")


class _FakeDb:
    def __init__(self):
        self.store: dict = {}

    def collection(self, name: str):
        return _FakeCollection(self.store, name)


@pytest.fixture(autouse=True)
def _prod_env(monkeypatch):
    """Ancre les `FIRM_*` sur les valeurs de production.

    `Config` lit l'environnement dans son CORPS DE CLASSE, une seule fois à
    l'import : patcher `os.environ` n'aurait aucun effet, il faut patcher
    les attributs de classe.
    """
    for key, value in _PROD.items():
        monkeypatch.setattr(Config, key, value, raising=True)


@pytest.fixture()
def fake_db(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(S, "db", db, raising=True)
    return db


def _seed_doc(db: _FakeDb, **over) -> dict:
    doc = dict(S._BLANK)
    doc.update(over)
    db.store[f"{S.COLLECTION}/{S.DOC_ID}"] = doc
    return doc


# ── La semence reproduit la production ───────────────────────────────────

def test_the_env_seed_reproduces_todays_rendered_values(fake_db):
    """Le critère d'acceptation du lot : aucune sortie ne bouge, sauf la
    province — correction assumée vers la convention que TOUTE adresse de
    contact de l'application suit déjà."""
    cab = cabinet_dict()
    assert cab["nom"] == "Me Jason Poirier Lavoie"
    assert cab["adresse_civique"] == "4970, chemin de la Côte-des-Neiges, suite 9"
    assert cab["ville"] == "Montréal"
    assert cab["code_postal"] == "H3V 1A4"
    assert cab["courriel"] == "reception@poirierlavoie.ca"
    # Le piège que ce lot devait éviter : stocker en E.164 sans rendre la
    # forme locale aurait ajouté « +1 » au téléphone du cabinet sur chaque
    # lettre, chaque procédure et le pied du PDF de budget.
    assert cab["telephone"] == "(514) 737-2525"
    assert cab["telecopieur"] == "(514) 737-6565"
    # La seule différence voulue.
    assert cab["province"] == "Québec"


def test_the_seed_normalizes_the_phone_to_e164():
    """Stocké canonique, affiché local — les deux moitiés du contrat."""
    seed = S._seed_from_env()
    assert seed["telephone"] == "+15147372525"
    assert seed["telecopieur"] == "+15147372525".replace("7372525", "7376565")
    assert seed["address_province"] == "Québec"


def test_the_seed_never_writes(fake_db):
    S._seed_from_env()
    assert fake_db.store == {}


# ── Les trois gardes de lecture ──────────────────────────────────────────

def test_get_cabinet_falls_back_to_the_seed_when_no_document(fake_db):
    assert fake_db.store == {}
    assert S.get_cabinet()["nom"] == "Me Jason Poirier Lavoie"


def test_get_cabinet_survives_a_firestore_error(monkeypatch):
    class _Boom:
        def collection(self, _name):
            raise RuntimeError("Firestore indisponible")

    monkeypatch.setattr(S, "db", _Boom(), raising=True)
    # Fail-open : chaque appelant rend ceci dans un document, un courriel ou
    # un PDF — un en-tête vide serait pire qu'une valeur de déploiement.
    assert S.get_cabinet()["nom"] == "Me Jason Poirier Lavoie"


def test_get_cabinet_refuses_a_non_dict_snapshot(monkeypatch):
    """La garde qui tient toute la suite honnête (voir le docstring)."""
    magic = mock.MagicMock()
    assert magic.get().exists          # le piège, démontré
    monkeypatch.setattr(S, "db", magic, raising=True)
    cab = S.get_cabinet()
    assert cab["nom"] == "Me Jason Poirier Lavoie"
    assert not any("MagicMock" in str(v) for v in cab.values())


# ── La règle « le document est TOUTE la vérité » ─────────────────────────

def test_a_blanked_field_is_not_resurrected_by_the_env_seed(fake_db):
    """`{**seed, **stored}` serait le piège de suppression en miroir :
    injecter une semence est une DÉ-suppression."""
    _seed_doc(fake_db, nom="Me X", telecopieur="")
    assert Config.FIRM_FAX == "(514) 737-6565"      # la semence en a un
    assert S.get_cabinet()["telecopieur"] == ""     # le juriste l'a effacé
    assert cabinet_dict()["telecopieur"] == ""


def test_an_unknown_stored_key_is_dropped(fake_db):
    _seed_doc(fake_db, nom="Me X")
    fake_db.store[f"{S.COLLECTION}/{S.DOC_ID}"]["surprise"] = "z"
    assert "surprise" not in S.get_cabinet()


def test_a_stored_list_is_coerced_to_a_string(fake_db):
    """Un chemin d'écriture DAV/vobject peut laisser une LISTE dans un champ
    texte ; chaque consommateur concatène ou rend la valeur."""
    _seed_doc(fake_db, nom="Me X", address_city=["Montréal", "QC"])
    assert S.get_cabinet()["address_city"] == "Montréal QC"


# ── La règle « _normalize ne pose jamais une clé absente » ───────────────

def test_normalize_never_injects_a_key_the_caller_did_not_supply(fake_db):
    """Assertion sur ce qui est STOCKÉ, à travers le vrai verbe du modèle —
    pas sur un dict remis à un mock, dont la frontière est précisément là où
    le défaut vivrait."""
    _seed_doc(
        fake_db, nom="Me X", telecopieur="+15147376565",
        address_street="1 rue A", address_city="Laval",
        address_province="Québec", address_country="Canada",
    )
    doc, errors = S.update_cabinet({"nom": "Me Y"})
    assert errors == []
    stored = fake_db.store[f"{S.COLLECTION}/{S.DOC_ID}"]
    assert stored["nom"] == "Me Y"
    # Rien d'autre n'a bougé — le défaut `mandataires` de models/partie.
    assert stored["telecopieur"] == "+15147376565"
    assert stored["address_city"] == "Laval"
    assert stored["address_street"] == "1 rue A"


def test_a_submitted_empty_field_does_erase(fake_db):
    """Le pendant de la règle : une clé PRÉSENTE et vide efface. C'est ce
    que le formulaire fait quand le juriste vide le télécopieur."""
    _seed_doc(fake_db, nom="Me X", telecopieur="+15147376565")
    doc, errors = S.update_cabinet({"nom": "Me X", "telecopieur": ""})
    assert errors == []
    assert fake_db.store[f"{S.COLLECTION}/{S.DOC_ID}"]["telecopieur"] == ""


def test_update_stores_e164_and_rule_7_fields(fake_db):
    doc, errors = S.update_cabinet(
        {"nom": "Me X", "telephone": "(438) 555-0100"}
    )
    assert errors == []
    assert doc["telephone"] == "+14385550100"
    assert doc["id"] == S.DOC_ID
    assert doc["created_at"] and doc["updated_at"] and doc["etag"]


def test_created_at_survives_a_second_save(fake_db):
    first, _ = S.update_cabinet({"nom": "Me X"})
    second, _ = S.update_cabinet({"nom": "Me Y"})
    assert second["created_at"] == first["created_at"]
    assert second["etag"] != first["etag"]


def test_a_legacy_province_code_is_migrated_on_save(fake_db):
    doc, errors = S.update_cabinet(
        {"nom": "Me X", "address_street": "1 rue A", "address_province": "QC"}
    )
    assert errors == []
    assert doc["address_province"] == "Québec"


# ── Validation ───────────────────────────────────────────────────────────

def test_nom_is_required(fake_db):
    doc, errors = S.update_cabinet({"nom": "   "})
    assert doc is None
    assert errors == ["Le nom du juriste est requis."]
    assert fake_db.store == {}          # rien d'écrit sur un refus


def test_an_invalid_phone_is_refused(fake_db):
    doc, errors = S.update_cabinet({"nom": "Me X", "telephone": "abc"})
    assert doc is None
    assert any("téléphone" in e for e in errors)


def test_an_invalid_postal_code_is_refused(fake_db):
    doc, errors = S.update_cabinet(
        {"nom": "Me X", "address_street": "1 rue A", "address_postal_code": "ZZZ"}
    )
    assert doc is None
    assert "Code postal invalide." in errors


def test_the_tax_numbers_are_never_format_validated(fake_db):
    """Ils s'impriment sur des factures émises : un refus à tort y est pire
    qu'une coquille, et l'application n'a jamais validé leur forme."""
    doc, errors = S.update_cabinet(
        {"nom": "Me X", "gst_number": "n'importe quoi", "qst_number": "1"}
    )
    assert errors == []
    assert doc["gst_number"] == "n'importe quoi"


def test_a_write_failure_returns_a_french_error(monkeypatch):
    class _SetBoom:
        @staticmethod
        def get():
            return _FakeSnap(None)

        @staticmethod
        def set(_data):
            raise RuntimeError("écriture refusée")

    class _Coll:
        @staticmethod
        def document(_doc_id):
            return _SetBoom()

    class _Db:
        @staticmethod
        def collection(_name):
            return _Coll()

    monkeypatch.setattr(S, "db", _Db(), raising=True)
    doc, errors = S.update_cabinet({"nom": "Me X"})
    assert doc is None
    assert errors == ["Erreur lors de la sauvegarde. Veuillez réessayer."]


# ── La forme de sortie, DÉRIVÉE et non recopiée ──────────────────────────

def test_cabinet_dict_exposes_exactly_the_declared_key_set(fake_db):
    """Épinglé par dérivation : un inventaire écrit à la main se périme."""
    assert set(cabinet_dict()) == set(CABINET_KEYS)


def test_changed_field_names_reports_names_only(fake_db):
    before = {"nom": "Me X", "telecopieur": "+1", "gst_number": ""}
    after = {"nom": "Me Y", "telecopieur": "+1", "gst_number": "RT0001"}
    assert S.changed_field_names(before, after) == ["gst_number", "nom"]


# ── Les deux anti-régressions structurelles ──────────────────────────────

def test_composer_cabinet_is_not_evaluated_at_import():
    """`main.py` importe ce module dans `create_app()`."""
    with mock.patch("google.cloud.firestore.Client"):
        import routes.taches_portail as tp
    assert not hasattr(tp, "_CABINET"), (
        "le profil du cabinet est de nouveau figé à l'import — une lecture "
        "Firestore dans create_app(), gelée pour la vie du worker"
    )


def test_the_organisation_seed_matches_the_taches_portail_literal():
    """Les deux copies du nom commercial ne peuvent plus dériver."""
    with mock.patch("google.cloud.firestore.Client"):
        import routes.taches_portail as tp
    assert tp._CABINET_REPLI["organisation"] == S.ORGANISATION_SEED


def test_the_portal_service_reads_no_settings_module():
    """Le portail ne peut pas atteindre la base par défaut (SA à portée
    « portail » seulement) et n'importe jamais `models`."""
    offenders = []
    for path in (_ROOT / "client").rglob("*.py"):
        src = io.open(path, encoding="utf-8").read()
        if re.search(r"\bfrom\s+models\b|\bimport\s+models\b", src):
            offenders.append(str(path.relative_to(_ROOT)))
        if "models.settings" in src or "cabinet_dict" in src:
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"le portail importe des règlages : {offenders}"


def test_no_route_or_template_reads_config_firm_any_more():
    """Le dernier lecteur de gabarit était le signataire de conformité ; il
    vit maintenant dans routes/parties.py.

    Le balayage lit le CODE, pas la prose : `ast` pour le Python (une
    mention en commentaire ou en docstring documente la migration et doit
    rester lisible), une recherche textuelle pour le Jinja, qui n'a pas
    d'arbre. `models/settings.py` est exclu : il EST la semence.
    """
    import ast

    offenders = []
    for folder in ("templates", "routes", "services", "utils", "mcp", "models"):
        for path in (_ROOT / folder).rglob("*"):
            rel = path.relative_to(_ROOT).as_posix()
            if rel == "models/settings.py":
                continue
            if path.suffix == ".py":
                tree = ast.parse(io.open(path, encoding="utf-8").read())
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in ("Config", "config")
                        and (
                            node.attr.startswith("FIRM_")
                            or node.attr in ("GST_NUMBER", "QST_NUMBER")
                        )
                    ):
                        offenders.append(f"{rel}:{node.lineno}")
            elif path.suffix == ".html":
                src_html = io.open(path, encoding="utf-8").read()
                for m in re.finditer(r"config\.(FIRM_|GST_NUMBER|QST_NUMBER)", src_html):
                    line = src_html[: m.start()].count(chr(10)) + 1
                    offenders.append(f"{rel}:{line}")
    assert not offenders, f"lecteurs directs restants : {offenders}"
