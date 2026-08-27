"""L'analyse documentaire persistée (SPEC Phase K §8).

La couche PURE (`utils/analyse_taxonomies`, `utils/analyse_protection`) a ses
propres tests. Ici on épingle la COUTURE : ce que le modèle peut et ne peut
pas faire écrire, et les garanties qui rendent tenable l'écart assumé avec la
§5.3 (l'analyse écrit directement dans `category`).
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
    import models.document as doc

from utils import analyse_taxonomies as tax  # noqa: E402


# ── Harnais ────────────────────────────────────────────────────────────────

class _FauxDoc:
    def __init__(self, store, key, sub=None):
        self.store, self.key = store, key
        self.sub = sub if sub is not None else {}

    def set(self, data):
        self.store[self.key] = dict(data)

    def collection(self, nom):
        return _FauxCollection(self.sub.setdefault(nom, {}))


class _FauxCollection:
    def __init__(self, store):
        self.store = store

    def document(self, key):
        return _FauxDoc(self.store, key)


@pytest.fixture()
def monde(monkeypatch):
    """Un document en place, et le journal observable."""
    store, journaux = {}, {}

    class _Racine:
        def document(self, key):
            return _FauxDoc(store, key, journaux.setdefault(key, {}))

    class _DB:
        def collection(self, nom):
            assert nom == doc.COLLECTION, nom
            return _Racine()

    monkeypatch.setattr(doc, "db", _DB())
    monkeypatch.setattr(
        doc, "get_document", lambda i: dict(store.get(i) or {}) or None
    )
    store["doc-1"] = {
        "id": "doc-1", "dossier_id": "d1", "category": "correspondance",
        "category_source": "juriste", "filename": "x.pdf",
        "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
    }
    return {"store": store, "journaux": journaux}


_SORTIE = {
    "sous_nature": "PROC_DEM_INTRO",
    "privileges": ["PUBLIC"],
    "resume": "Demande introductive d'instance.",
    "numero_dossier_cour": "500-17-123456-250",
    "tribunal": "Cour superieure",
    "district_judiciaire": "Montreal",
    "auteur": "Me X",
    "date_document_str": "2026-03-14",
    "parties_mentionnees": ["Tremblay", "Gagnon"],
}


# ── La garantie centrale : la catégorie est DÉRIVÉE ────────────────────────

def test_the_category_is_derived_never_supplied():
    """LA garantie qui rend l'écart avec la §5.3 tenable.

    Le modèle fournit une sous-nature d'une table FERMÉE; le code en dérive
    la catégorie. Une catégorie inventée est structurellement impossible —
    et un champ `category` glissé dans la sortie n'a aucun effet.
    """
    champ, err = doc._analyse_derivee(
        {**_SORTIE, "category": "facture", "nature_detectee": "facture"},
        document={"category": "correspondance"},
    )
    assert err == []
    assert champ["nature_detectee"] == tax.nature_of("PROC_DEM_INTRO")
    assert champ["nature_detectee"] == "procédure"
    # Et la valeur dérivée est toujours une catégorie de SAISIE valide.
    assert champ["nature_detectee"] in doc.CATEGORY_CHOICES


def test_unknown_codes_are_refused_never_coerced():
    for sortie, attendu in (
        ({"sous_nature": "INVENTE"}, "Sous-nature inconnue"),
        ({"sous_nature": ""}, "Sous-nature inconnue"),
        ({"sous_nature": "PROC_DEM_INTRO", "privileges": ["MAGIQUE"]},
         "Privilège inconnu"),
    ):
        champ, err = doc._analyse_derivee(sortie, document={})
        assert champ == {} and err, sortie
        assert attendu in err[0], err


def test_confirme_is_never_true_by_derivation():
    """§7 : aucun chemin automatique ne confirme une qualification."""
    champ, _ = doc._analyse_derivee(_SORTIE, document={})
    assert champ["confirme"] is False
    assert champ["confirme_par"] is None and champ["confirme_le"] is None


# ── L'écriture ─────────────────────────────────────────────────────────────

def test_record_writes_category_source_and_journals_what_it_replaced(monde):
    """L'écrasement est assumé — mais rien ne se perd.

    En écrasant, on détruit la comparaison à deux valeurs dont vivait
    `divergence_categorie`. Le journal garde donc la catégorie précédente ET
    sa source, et l'avertissement ne se lève que sur un choix HUMAIN.
    """
    maj, err = doc.record_analyse("doc-1", _SORTIE, modele="claude-opus-5")
    assert err == []
    assert maj["category"] == "procédure"
    assert maj["category_source"] == "analyse"

    a = maj["analyse"]
    assert a["categorie_precedente"] == "correspondance"
    assert a["categorie_precedente_source"] == "juriste"
    assert a["categorie_remplacee"] is True
    assert a["remplace_un_choix_du_juriste"] is True

    journal = monde["journaux"]["doc-1"][doc.ANALYSES_SUBCOLLECTION]
    assert len(journal) == 1
    assert list(journal.values())[0]["analyse_id"] == a["analyse_id"]


def test_replacing_a_versement_default_raises_no_warning(monde):
    """Un « autre » posé par défaut au versement du portail n'est pas un
    choix du juriste : l'écraser ne mérite aucun avertissement, sans quoi la
    reprise d'un fonds en produirait un par document."""
    monde["store"]["doc-1"]["category"] = "autre"
    monde["store"]["doc-1"]["category_source"] = "analyse"
    maj, err = doc.record_analyse("doc-1", _SORTIE)
    assert err == []
    assert maj["analyse"]["categorie_remplacee"] is True
    assert maj["analyse"]["remplace_un_choix_du_juriste"] is False


def test_the_journal_is_append_only_across_runs(monde):
    for _ in range(3):
        _, err = doc.record_analyse("doc-1", _SORTIE)
        assert err == []
    journal = monde["journaux"]["doc-1"][doc.ANALYSES_SUBCOLLECTION]
    assert len(journal) == 3, "chaque exécution laisse SA trace"
    assert len({e["analyse_id"] for e in journal.values()}) == 3


def test_no_delete_verb_touches_the_journal():
    """Doctrine « aucune suppression », et condition d'auditabilité : sans
    historique, on ne peut pas constater qu'un niveau de protection a
    baissé."""
    import inspect
    src = inspect.getsource(doc)
    for ligne in src.splitlines():
        if "ANALYSES_SUBCOLLECTION" in ligne:
            assert ".delete(" not in ligne, ligne


def test_record_refuses_an_unknown_document(monde):
    maj, err = doc.record_analyse("inconnu", _SORTIE)
    assert maj is None and err == ["Document introuvable."]


def test_no_ctag_bump_on_this_path():
    """`documents` n'est pas exposée en DAV. Un bump copié d'un autre modèle
    ferait resynchroniser DavX5 pour rien."""
    import ast
    import inspect
    # Un contrôle d'APPEL, jamais de sous-chaîne : la première version de ce
    # test attrapait la docstring qui dit « aucun bump_ctag », donc il
    # rougissait sur la prose et serait resté vert sur un vrai appel écrit
    # autrement (`sync.bump_ctag`, un alias).
    for fn in (doc.record_analyse, doc.confirmer_analyse):
        arbre = ast.parse(inspect.getsource(fn).lstrip())
        appels = {
            n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
            for n in ast.walk(arbre) if isinstance(n, ast.Call)
        }
        assert "bump_ctag" not in appels, fn.__name__
        assert not any("ctag" in a for a in appels), (fn.__name__, appels)


# ── La confirmation ────────────────────────────────────────────────────────

def test_confirmation_is_the_only_path_and_clears_the_presumption(monde):
    doc.record_analyse("doc-1", _SORTIE)
    maj, err = doc.confirmer_analyse("doc-1", "Me Jason Poirier Lavoie")
    assert err == []
    assert maj["analyse"]["confirme"] is True
    assert maj["analyse"]["confirme_le"] is not None
    # La confirmation fait de la catégorie une détermination de l'avocat.
    assert maj["category_source"] == "juriste"


def test_confirmation_refuses_when_there_is_nothing_to_confirm(monde):
    maj, err = doc.confirmer_analyse("doc-1", "Me X")
    assert maj is None and err == ["Aucune analyse à confirmer."]


# ── L'outil MCP ────────────────────────────────────────────────────────────

_ARGS = {
    "document_id": "doc-1", "sous_nature": "PROC_DEM_INTRO",
    "privileges": ["PUBLIC"], "resume": "Demande introductive.",
    "numero_dossier_cour": "500-17-1", "tribunal": "C.S.",
    "district_judiciaire": "Montreal", "auteur": "Me X",
    "date_document_str": "2026-03-14", "parties_mentionnees": ["A"],
}


def _handlers():
    import mcp.handlers as h
    return h


def test_the_tool_declares_no_category_parameter():
    """La garantie, au niveau du SCHÉMA.

    Ajouter `category` aux entrées rouvrirait exactement ce que l'écart
    assumé avec la §5.3 rend supportable : le modèle choisirait la valeur au
    lieu de la voir dérivée d'un code fermé.
    """
    import mcp.tools as t
    props = t.TOOLS["record_document_analysis"]["input_schema"]["properties"]
    for interdit in ("category", "nature_detectee", "niveau_protection",
                     "confirme", "famille"):
        assert interdit not in props, interdit
    assert props["sous_nature"]["enum"] == sorted(tax.VALID_SOUS_NATURES)
    assert props["privileges"]["items"]["enum"] == sorted(tax.VALID_PRIVILEGES)


def test_the_tool_is_a_write_that_warns():
    import mcp.tools as t
    n = "record_document_analysis"
    assert n in t.WRITE_TOOLS and n in t.EDIT_TOOLS
    assert t.required_scope(n) == "athena:write"
    d = [x for x in t.list_tool_descriptors(
        frozenset({"athena:read", "athena:write"})) if x["name"] == n][0]
    # destructiveHint est DÉRIVÉ d'EDIT_TOOLS : il remplace une valeur que
    # l'explorateur affiche et sur laquelle le juriste filtre.
    assert d["annotations"]["destructiveHint"] is True
    assert d["annotations"]["idempotentHint"] is False


def test_the_dry_branch_refuses_what_the_live_call_refuses(monde, monkeypatch):
    """`run_write` court-circuite la branche sèche SANS appeler le modèle.

    Toute garde du modèle qu'un appelant peut déclencher doit donc être
    rejouée dans le gestionnaire, AVANT cette branche — sans quoi une
    simulation annonce un succès que l'appel réel refuse.
    """
    h = _handlers()
    monkeypatch.setattr(h.document_model, "get_document",
                        lambda i: dict(monde["store"].get(i) or {}) or None)
    with pytest.raises(Exception) as exc:
        h.record_document_analysis({**_ARGS, "sous_nature": "INVENTE",
                                    "dry_run": True})
    assert "Sous-nature inconnue" in str(exc.value)

    with pytest.raises(Exception) as exc2:
        h.record_document_analysis({**_ARGS, "document_id": "inconnu",
                                    "dry_run": True})
    assert "introuvable" in str(exc2.value)


def test_the_dry_branch_writes_nothing_and_conforms(monde, monkeypatch):
    import mcp.output_schemas as o
    import mcp.tools as t
    h = _handlers()
    monkeypatch.setattr(h.document_model, "get_document",
                        lambda i: dict(monde["store"].get(i) or {}) or None)
    avant = dict(monde["store"]["doc-1"])
    r = h.record_document_analysis({**_ARGS, "dry_run": True})

    assert r["recorded"] is False
    assert monde["store"]["doc-1"] == avant, "la simulation a écrit"
    assert not monde["journaux"].get("doc-1"), "la simulation a journalisé"
    # Le contrat outputSchema, sur la charge RÉELLE du gestionnaire.
    assert t.validate_args(o.OUTPUT_SCHEMAS["record_document_analysis"], r) == []
    # L'avertissement de présomption est TOUJOURS là — c'est ce qui remplace
    # le second clic que la §5.3 exigeait.
    assert any("PRÉSUMÉE" in w for w in r["warnings"])


# ── L'écran (SPEC Phase K §9.1) ────────────────────────────────────────────

def _env():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader
    racine = Path(__file__).resolve().parent.parent / "templates"
    e = Environment(loader=FileSystemLoader(str(racine)))
    e.globals.update(ms=lambda n, **k: "", url_for=lambda *a, **k: "#",
                     csrf_token=lambda: "x")
    e.filters["to_mtl"] = lambda d: d
    return e


_DOC_RENDU = {
    "id": "d1", "display_name": "PV.pdf", "category": "procédure",
    "category_source": "analyse",
    "analyse": {
        "sous_nature": "PROC_DEM_INTRO", "nature_detectee": "procédure",
        "famille": "JUDICIAIRE", "privileges": ["LITIGE"],
        "niveau_protection": 2, "confirme": False,
        "resume": "Demande introductive.",
        "categorie_precedente": "correspondance",
        "remplace_un_choix_du_juriste": True,
        "champs_attendus_absents": ["date_document_str"],
        "alerte_renonciation_possible": True,
    },
}


def test_the_screen_leads_with_protection_then_nature():
    """§9.1 : le niveau de protection AVANT la nature.

    C'est lui qui commande la manipulation du document — ce qui peut être
    transmis, à qui. L'ordre n'est pas cosmétique.
    """
    html = _env().get_template("documents/_analyse.html").render(
        document=_DOC_RENDU, analyses=[]
    )
    assert html.index("Privilégié") < html.index("Nature détectée")


def test_the_presumption_is_shown_not_hidden():
    """§7 nº 3 : la mention accompagne la valeur. C'est elle qui remplace le
    second clic que la §5.3 exigeait."""
    html = _env().get_template("documents/_analyse.html").render(
        document=_DOC_RENDU, analyses=[]
    )
    assert "Présumée" in html
    assert "Confirmer cette classification" in html

    confirme = {**_DOC_RENDU,
                "analyse": {**_DOC_RENDU["analyse"], "confirme": True}}
    html2 = _env().get_template("documents/_analyse.html").render(
        document=confirme, analyses=[]
    )
    assert "Présumée" not in html2
    assert "Confirmer cette classification" not in html2


def test_every_alert_is_displayed_never_folded():
    """Une alerte qu'il faut déplier est une alerte qu'on ne lit pas."""
    html = _env().get_template("documents/_analyse.html").render(
        document=_DOC_RENDU, analyses=[]
    )
    for attendu in ("a remplacé la catégorie", "Mentions attendues absentes",
                    "Renonciation possible"):
        assert attendu in html, attendu
        # Aucune ne vit dans un <details>.
        avant = html[:html.index(attendu)]
        assert avant.count("<details") == avant.count("</details>"), attendu


def test_a_document_without_analysis_renders_nothing():
    html = _env().get_template("documents/_analyse.html").render(
        document={"id": "d1"}, analyses=[]
    )
    assert html.strip() == ""


def test_the_screen_uses_only_compiled_classes():
    """Une classe absente de l'artefact ne s'applique pas — en silence."""
    import re
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent
    css = next(racine.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    html = _env().get_template("documents/_analyse.html").render(
        document=_DOC_RENDU, analyses=[]
    )
    classes = set()
    for bloc in re.findall(r'class="([^"]+)"', html):
        classes.update(c for c in bloc.split() if c and not c.startswith("{"))
    # Tailwind v4 échappe le POINT autant que les deux-points : `py-0.5`
    # sort en `.py-0\.5`. Un contrôleur qui l'oublie déclare absentes des
    # classes que les gabarits existants emploient depuis toujours.
    def echappe(c):
        for brut, ech in ((chr(92), chr(92) * 2), (':', chr(92) + ':'),
                          ('.', chr(92) + '.'), ('/', chr(92) + '/'),
                          ('[', chr(92) + '['), (']', chr(92) + ']')):
            c = c.replace(brut, ech)
        return c

    absentes = [c for c in sorted(classes) if ('.' + echappe(c)) not in css]
    assert not absentes, f"classes absentes de l'artefact : {absentes}"


def test_the_confirm_route_is_the_only_one_that_confirms():
    """Un balayage : aucune autre route ne lève `confirme`."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "routes" / "documents.py").read_text(encoding="utf-8")
    assert "def analyse_confirmer" in src
    appels = re.findall(r"confirmer_analyse\(", src)
    assert len(appels) == 1, "un second appelant confirmerait ailleurs"
