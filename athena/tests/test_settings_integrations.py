"""Les réglages d'intégration — `settings/integrations` et la ligne 9/4.

Ce fichier épingle les propriétés dont dépendent les lots suivants, et
plusieurs valent d'être nommées ici parce qu'elles échouent EN SILENCE :

* **La correspondance champ ↔ attribut `Config` est DÉRIVÉE**, jamais
  transcrite. Un inventaire recopié à la main est exactement ce que ce dépôt
  suit comme dette pour chaque vocabulaire dupliqué ; ici il se périmerait au
  premier champ ajouté, et la semence rendrait alors `None` sans que rien ne
  le dise.
* **`_read_state` distingue ABSENT d'ILLISIBLE.** Les deux rendent la
  semence, mais la politique de l'appelant diffère : un document absent est
  un déploiement neuf (tourner normalement), un magasin illisible doit
  DÉSARMER la boucle d'annulation et refuser une écriture. Confondre les deux
  gèlerait les deux synchros au premier démarrage.
* **La garde `isinstance(data, dict)`** : un instantané `MagicMock` a un
  `.exists` VRAI et rend un `MagicMock` de `.to_dict()`. Sans elle, des
  objets muets entreraient dans un prédicat de mots-clés et la plupart des
  assertions passeraient quand même — un faux magasin qui accepte ce que le
  vrai refuse ne prouve rien.
* **Les clés de la carte sont PLIÉES** avec la même fonction que le
  prédicat. Une clé non pliée ne peut jamais mordre, en silence, sur une page
  qui a l'air d'avoir accepté la valeur.
"""

import ast
import io
import os
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
    from models import integrations as I
    from models.hearing import VALID_HEARING_TYPES_EXTRAJUDICIAIRE
    from utils import integrations_defaults as D

_ROOT = Path(__file__).resolve().parents[1]


# ── Un faux magasin, pas un MagicMock ──────────────────────────────────────
#
# Le motif du dépôt : un faux qui STOCKE des octets et refuse ce que Firestore
# refuse. Un MagicMock accepterait un tuple imbriqué que le vrai magasin
# rejette, et le défaut ne paraîtrait qu'au premier appel réel.

class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Doc:
    def __init__(self, store, key):
        self._store, self._key = store, key

    def get(self):
        if self._store.get("__raise__"):
            raise RuntimeError("firestore down")
        return _Snap(self._store.get(self._key))

    def set(self, payload):
        if self._store.get("__raise_write__"):
            raise RuntimeError("firestore down")
        for k, v in payload.items():
            if isinstance(v, tuple):
                raise TypeError(
                    f"Firestore refuse un tuple ({k}) — il faut une liste"
                )
        self._store[self._key] = dict(payload)


class _Coll:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return _Doc(self._store, key)


class _DB:
    def __init__(self, store):
        self._store = store

    def collection(self, _name):
        return _Coll(self._store)


@pytest.fixture()
def store(monkeypatch):
    st: dict = {}
    monkeypatch.setattr(I, "db", _DB(st), raising=True)
    return st


def _valid(**over) -> dict:
    base = {
        "bookings_subject_keywords": ("Consultation", "Rencontre"),
        "bookings_type_par_mot_cle": {
            "consultation": "consultation",
            "rencontre": "rencontre",
        },
        "bookings_type_defaut": "consultation",
        "bookings_sync_lookahead_days": 90,
        "bookings_sync_lookback_days": 1,
        "bookings_debug_payload": False,
        "miroir_outlook_lookahead_days": 365,
        "miroir_outlook_lookback_days": 30,
        "graph_sender_name": "Poirier Lavoie, avocat",
    }
    base.update(over)
    return base


# ── L'inventaire, épinglé PAR DÉRIVATION ──────────────────────────────────

def test_every_field_maps_to_a_real_config_attribute():
    """La semence fait `getattr(Config, name.upper())`. Un champ sans
    attribut correspondant rendrait `None` en silence, et la valeur par
    défaut d'un compteur de jours deviendrait `None` — puis `parse_int` la
    replierait sur le défaut, donc RIEN ne signalerait l'erreur."""
    for name in I._FIELDS:
        assert hasattr(Config, name.upper()), f"Config.{name.upper()} absent"


def test_the_nine_four_line_is_exhaustive_and_disjoint():
    """Les treize valeurs de la couche A, plus les deux coupe-circuits, et
    aucun chevauchement — un champ dans les deux listes serait rendu deux
    fois, une fois modifiable et une fois non."""
    editable = set(D.EDITABLE_FIELDS)
    deploy = set(D.DEPLOY_ONLY_FIELDS)
    kill = set(D.KILL_SWITCHES)
    assert len(editable) == 9
    assert len(deploy) == 4
    assert not editable & deploy
    assert not editable & kill
    assert not deploy & kill
    for name in deploy | kill:
        assert hasattr(Config, name.upper()), f"Config.{name.upper()} absent"


def test_the_two_classmethod_inputs_are_all_deploy_only():
    """LE DIVIDENDE STRUCTUREL, et la raison que cette ligne est la bonne
    plutôt que simplement plus petite.

    `graph_configured()` lit TENANT_ID + CLIENT_ID + CLIENT_SECRET +
    SENDER_UPN ; `bookings_configured()` ajoute BOOKINGS_JURISTE_UPN. Si
    l'une de ces cinq passait du côté modifiable, il faudrait un accesseur
    sur `Config` — or `Config` n'est JAMAIS instancié, donc un `@property`
    lu sur la classe rend l'OBJET property, qui est VRAI, et
    `graph_configured()` répondrait True sans aucun identifiant. Fail-open,
    en silence. Ce test est ce qui garde ce chemin inexistant."""
    inputs = {
        "graph_tenant_id", "graph_client_id", "graph_client_secret",
        "graph_sender_upn", "bookings_juriste_upn",
    }
    assert not inputs & set(D.EDITABLE_FIELDS)


def test_seed_reproduces_config():
    seed = I._seed_from_config()
    assert set(seed) == set(I._FIELDS)
    for name in I._FIELDS:
        assert seed[name] == getattr(Config, name.upper())


# ── Les trois états de lecture ────────────────────────────────────────────

def test_absent_document_seeds_and_says_absent(store):
    rec, prov = I.get_integrations_state()
    assert prov == "absent"
    assert rec["bookings_type_defaut"] == Config.BOOKINGS_TYPE_DEFAUT


def test_unreadable_store_seeds_and_says_unreadable(store):
    store["__raise__"] = True
    rec, prov = I.get_integrations_state()
    assert prov == "unreadable"
    assert rec["bookings_subject_keywords"] == Config.BOOKINGS_SUBJECT_KEYWORDS


def test_a_non_mapping_payload_is_unreadable_not_ok(store):
    """La garde MagicMock. `.exists` vrai + `.to_dict()` non-dict doit se
    lire ILLISIBLE, jamais « ok » — sans quoi des objets muets entrent dans
    le prédicat."""
    store["integrations"] = ["pas", "un", "mapping"]
    rec, prov = I.get_integrations_state()
    assert prov == "unreadable"
    assert isinstance(rec, dict)


def test_absent_is_not_confused_with_unreadable(store):
    """Les deux rendent la semence ; seule la provenance les distingue, et
    c'est elle qui décide si la boucle d'annulation reste armée."""
    _, absent = I.get_integrations_state()
    store["__raise__"] = True
    _, unreadable = I.get_integrations_state()
    assert absent == "absent"
    assert unreadable == "unreadable"
    assert absent != unreadable


# ── Écriture ──────────────────────────────────────────────────────────────

def test_write_then_read_round_trips(store):
    doc, errors = I.update_integrations(_valid())
    assert errors == []
    rec, prov = I.get_integrations_state()
    assert prov == "store"
    assert rec["bookings_subject_keywords"] == ("Consultation", "Rencontre")
    assert rec["miroir_outlook_lookahead_days"] == 365


def test_keywords_are_stored_as_a_list_never_a_tuple(store):
    """Firestore refuse un tuple ; le faux magasin le refuse aussi, donc ce
    test échoue si la conversion disparaît."""
    _, errors = I.update_integrations(_valid())
    assert errors == []
    assert isinstance(store["integrations"]["bookings_subject_keywords"], list)


def test_write_is_REFUSED_while_the_store_is_unreadable(store):
    """Règle 3. `models/settings.update_cabinet` fait
    `_read_raw() or _seed_from_env()` comme base de fusion — inoffensif là
    parce que le formulaire poste ses treize champs. Ici cela fusionnerait la
    semence de déploiement par-dessus ce que le magasin contient vraiment."""
    store["__raise__"] = True
    doc, errors = I.update_integrations(_valid())
    assert doc is None
    assert any("illisible" in e for e in errors)
    assert "integrations" not in store


def test_a_cleared_display_name_stays_cleared(store):
    """Règle 1 : une fois le document créé il est TOUTE la vérité. Un
    `{**semence, **stocké}` ressusciterait la valeur de déploiement pour un
    champ délibérément vidé."""
    I.update_integrations(_valid(graph_sender_name="Cabinet"))
    I.update_integrations(_valid(graph_sender_name=""))
    rec, _ = I.get_integrations_state()
    assert rec["graph_sender_name"] == ""


def test_normalize_is_presence_gated(store):
    """Règle 2 : `update_integrations` fusionne puis écrit le document
    complet, donc une clé injectée par le normaliseur EST une suppression —
    le défaut que `models/partie._normalize` a livré."""
    partial = {"graph_sender_name": "X"}
    out = I._normalize(dict(partial))
    assert set(out) == {"graph_sender_name"}


def test_a_partial_save_keeps_the_other_stored_values(store):
    I.update_integrations(_valid(miroir_outlook_lookback_days=45))
    doc, errors = I.update_integrations({"graph_sender_name": "Autre"})
    assert errors == []
    assert doc["miroir_outlook_lookback_days"] == 45
    assert doc["graph_sender_name"] == "Autre"


# ── Refus ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["", " , ", ",,", "   "])
def test_an_empty_keyword_set_is_refused(store, raw):
    """La panne totale silencieuse de la contrainte 6, refusée à l'écriture."""
    doc, errors = I.update_integrations(
        _valid(bookings_subject_keywords=D.parse_keywords(raw))
    )
    assert doc is None
    assert any("mot-clé" in e for e in errors)
    assert "integrations" not in store


def test_the_refusal_names_the_consequence(store):
    _, errors = I.update_integrations(_valid(bookings_subject_keywords=()))
    joined = " ".join(errors)
    assert "annulées côté client" in joined


def test_a_keyword_containing_a_comma_is_refused(store):
    """Le champ est séparé par des virgules : une virgule dans un mot-clé est
    irreprésentable sur l'aller-retour."""
    doc, errors = I.update_integrations(
        _valid(bookings_subject_keywords=("Consultation, longue",))
    )
    assert doc is None
    assert any("virgule" in e for e in errors)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("bookings_sync_lookahead_days", 0),
        ("bookings_sync_lookahead_days", 1826),
        ("miroir_outlook_lookahead_days", 0),
        ("miroir_outlook_lookahead_days", 99999),
        ("bookings_sync_lookback_days", -1),
        ("miroir_outlook_lookback_days", 366),
    ],
)
def test_day_counts_are_range_checked(store, field, bad):
    doc, errors = I.update_integrations(_valid(**{field: bad}))
    assert doc is None
    assert any(field in e for e in errors)


def test_a_lookahead_of_zero_is_refused_not_accepted(store):
    """Zéro réduirait le miroir Outlook à ne rien maintenir — ce n'est pas
    une valeur qu'un juriste peut vouloir dire."""
    doc, _ = I.update_integrations(_valid(miroir_outlook_lookahead_days=0))
    assert doc is None


def test_a_judiciaire_type_is_refused_in_the_map(store):
    """`forum_of` DÉRIVE le forum du type, donc une valeur judiciaire ici
    ferait lire `forum="judiciaire"` à une consultation client partout."""
    doc, errors = I.update_integrations(
        _valid(bookings_type_par_mot_cle={"consultation": "audience"})
    )
    assert doc is None
    assert any("extrajudiciaire" in e for e in errors)


def test_a_judiciaire_default_type_is_refused(store):
    doc, errors = I.update_integrations(_valid(bookings_type_defaut="instruction"))
    assert doc is None
    assert any("extrajudiciaire" in e for e in errors)


def test_an_empty_default_type_is_refused(store):
    doc, errors = I.update_integrations(_valid(bookings_type_defaut=""))
    assert doc is None
    assert any("défaut" in e for e in errors)


def test_every_extrajudiciaire_type_is_accepted(store):
    """Le refus doit porter sur le vocabulaire réel, pas sur un sous-ensemble
    transcrit : chacune des cinq valeurs doit passer."""
    for t in VALID_HEARING_TYPES_EXTRAJUDICIAIRE:
        doc, errors = I.update_integrations(_valid(bookings_type_defaut=t))
        assert errors == [], f"{t} refusé : {errors}"


# ── Le pliage ─────────────────────────────────────────────────────────────

def test_map_keys_are_folded_with_the_predicate_fold(store):
    """Une clé accentuée non pliée ne mordrait JAMAIS, en silence."""
    doc, errors = I.update_integrations(
        _valid(bookings_type_par_mot_cle={"Médiation": "rencontre"})
    )
    assert errors == []
    assert "mediation" in doc["bookings_type_par_mot_cle"]


def test_the_fold_is_the_one_the_predicate_uses():
    """Pas une seconde implémentation : `graph_calendrier._plier` DOIT être
    cette fonction, sans quoi la carte stockée serait pliée d'une façon et
    consultée d'une autre."""
    from utils import graph_calendrier as gc
    assert gc._plier is D.plier


def test_the_fold_is_NOT_the_rapprochement_fold():
    """Elles se ressemblent et ne sont pas les mêmes : celle de
    `rapprochement` réduit en plus la ponctuation à des espaces. Les fusionner
    changerait le rapprochement de noms du contrôle de conflits."""
    from utils import rapprochement as R
    assert R._plier("Béton-Nord inc.") != D.plier("Béton-Nord inc.")


def test_nfc_and_nfd_fold_to_the_same_key():
    assert D.plier("Réunion") == D.plier("Réunion")


# ── L'avertissement (jamais un refus) ─────────────────────────────────────

def test_an_unmapped_keyword_warns_but_does_not_refuse(store):
    doc, errors = I.update_integrations(
        _valid(
            bookings_subject_keywords=("Consultation", "Médiation"),
            bookings_type_par_mot_cle={"consultation": "consultation"},
        )
    )
    assert errors == []
    assert I.unmapped_keywords(doc) == ["Médiation"]


def test_a_fully_mapped_record_warns_about_nothing(store):
    doc, _ = I.update_integrations(_valid())
    assert I.unmapped_keywords(doc) == []


# ── Le journal d'événement ────────────────────────────────────────────────

def test_changed_field_names_returns_names_only():
    before = _valid()
    after = _valid(graph_sender_name="Autre", miroir_outlook_lookback_days=45)
    names = I.changed_field_names(before, after)
    assert names == ["graph_sender_name", "miroir_outlook_lookback_days"]
    for n in names:
        assert "Autre" not in n


# ── Le module de défauts reste PUR ────────────────────────────────────────

def test_integrations_defaults_imports_only_stdlib():
    """Épinglé sur l'AST, pas sur une lecture. C'est cette propriété qui
    permet à `config.py` de l'importer dans son corps de classe : un import
    de `models` y mettrait la construction d'un client Firestore, et les
    quatre fichiers de test Graph sans identifiants cesseraient d'être
    importables."""
    src = io.open(_ROOT / "utils" / "integrations_defaults.py", encoding="utf-8").read()
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    forbidden = mods & {"models", "config", "flask", "google", "security"}
    assert not forbidden, f"imports interdits : {forbidden}"
    assert mods <= {"unicodedata", "typing", "re"}, mods


def test_defaults_cover_every_editable_field():
    assert set(D.DEFAULTS) == set(D.EDITABLE_FIELDS)


def test_blank_never_yields_a_zero_day_count():
    """`_BLANK = DEFAULTS` est une dérogation raisonnée : elle n'est sûre que
    parce qu'aucun compteur n'y vaut 0."""
    for key in D.JOURS_FIELDS:
        low, _ = D.JOURS_BORNES[key]
        assert I._BLANK[key] >= low >= 0
        assert I._BLANK[key] > 0 or low == 0


# ── Les parseurs (l'aller-retour du formulaire) ───────────────────────────

def test_keyword_round_trip():
    raw = "Consultation, Rencontre"
    assert D.format_keywords(D.parse_keywords(raw)) == raw


def test_type_map_round_trip():
    raw = "consultation = consultation\nrencontre = rencontre"
    assert D.format_type_map(D.parse_type_map(raw)) == raw


def test_a_type_map_line_without_equals_is_skipped_not_guessed():
    assert D.parse_type_map("consultation\nrencontre = rencontre") == {
        "rencontre": "rencontre"
    }


def test_parse_int_refuses_a_bool():
    """`int(True)` vaut 1 : une valeur de case à cocher arrivant dans un
    compteur de jours ne doit pas devenir « un jour » en silence."""
    assert D.parse_int(True, 90) == 90
    assert D.parse_int("45", 90) == 45
    assert D.parse_int("bruit", 90) == 90
