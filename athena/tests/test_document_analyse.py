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


def test_the_handler_refuses_before_it_writes(monde, monkeypatch):
    """Le gestionnaire rejoue les gardes du modèle AVANT toute écriture.

    Ces gardes sont nées de la branche sèche — `run_write` la
    court-circuitait sans appeler le modèle, donc une simulation aurait
    annoncé un succès que l'appel réel refusait. La branche est partie le
    2026-08-27 ; les gardes restent, et elles comptent toujours : un refus
    nommé en français vaut mieux qu'une erreur de modèle, et surtout rien
    ne doit être écrit ni journalisé.
    """
    h = _handlers()
    monkeypatch.setattr(h.document_model, "get_document",
                        lambda i: dict(monde["store"].get(i) or {}) or None)

    def _interdit(*a, **k):
        raise AssertionError("un refus a atteint le modèle")

    monkeypatch.setattr(h.document_model, "record_analyse", _interdit)
    avant = dict(monde["store"]["doc-1"])

    with pytest.raises(Exception) as exc:
        h.record_document_analysis({**_ARGS, "sous_nature": "INVENTE"})
    assert "Sous-nature inconnue" in str(exc.value)

    with pytest.raises(Exception) as exc2:
        h.record_document_analysis({**_ARGS, "document_id": "inconnu"})
    assert "introuvable" in str(exc2.value)

    assert monde["store"]["doc-1"] == avant
    assert not monde["journaux"].get("doc-1")


# ── L'écran (SPEC Phase K §9.1) ────────────────────────────────────────────

def _env():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader
    racine = Path(__file__).resolve().parent.parent / "templates"
    e = Environment(loader=FileSystemLoader(str(racine)))
    e.globals.update(ms=lambda *a, **k: "", url_for=lambda *a, **k: "#",
                     csrf_token=lambda *a, **k: "x")
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


def test_the_regime_badge_sits_beside_the_category():
    """Le régime se lit dans l'EN-TÊTE, à côté de la catégorie.

    C'est là qu'on le cherche, et il y coûte une ligne au lieu d'une carte.
    Il commande la manipulation du document, donc il précède tout le reste
    — y compris le bloc d'analyse, qui vient plus bas.
    """
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent / 'templates' / 'documents'
    detail = (racine / 'detail.html').read_text(encoding='utf-8')
    bloc = (racine / '_analyse.html').read_text(encoding='utf-8')

    # La pastille est dans l'en-tête, juste après celle de catégorie…
    i_cat = detail.index('category_labels.get(document.category')
    i_prot = detail.index('niveau_protection')
    i_analyse = detail.index('documents/_analyse.html')
    assert i_cat < i_prot < i_analyse
    # …et PAS dupliquée dans le bloc.
    assert 'niveau_protection' not in bloc.split('{# ── Ce qui doit')[0] or \
        'niveaux' not in bloc

def test_the_presumption_is_shown_not_hidden():
    """§7 nº 3 : la mention accompagne la valeur. C'est elle qui remplace le
    second clic que la §5.3 exigeait."""
    html = _env().get_template("documents/_analyse.html").render(
        document=_DOC_RENDU, analyses=[]
    )
    assert "Présumée" in html
    assert "Confirmer" in html

    confirme = {**_DOC_RENDU,
                "analyse": {**_DOC_RENDU["analyse"], "confirme": True}}
    html2 = _env().get_template("documents/_analyse.html").render(
        document=confirme, analyses=[]
    )
    assert "Présumée" not in html2
    assert "Confirmer" not in html2


def test_every_alert_is_displayed_never_folded():
    """Une alerte qu'il faut déplier est une alerte qu'on ne lit pas."""
    html = _env().get_template("documents/_analyse.html").render(
        document=_DOC_RENDU, analyses=[]
    )
    for attendu in ("Remplace la catégorie", "Mentions attendues absentes",
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


def test_the_live_write_path_runs_end_to_end(monde, monkeypatch):
    """Le chemin d'écriture RÉEL.

    Vécu le 2026-08-27, du temps où une branche sèche existait : les 3 033
    tests étaient verts et l'écriture plantait en production, parce
    qu'aucun ne parcourait la branche vivante — la ligne de journal posée
    APRÈS le commit appelait `log_mcp_event` sans son argument `outcome`,
    et le juriste a lu « échec » sur une analyse parfaitement enregistrée.
    La branche sèche est partie ; ce test reste, il n'y a plus qu'un
    chemin et c'est celui-ci.
    """
    h = _handlers()
    monkeypatch.setattr(h.document_model, "get_document",
                        lambda i: dict(monde["store"].get(i) or {}) or None)
    monkeypatch.setattr(h.document_model, "record_analyse",
                        doc.record_analyse)
    monkeypatch.setattr(h.dossier_model, "get_dossier", lambda i: None)

    r = h.record_document_analysis(dict(_ARGS))

    assert r["recorded"] is True
    assert r["category"] == "procédure"
    assert r["category_source"] == "analyse"
    assert monde["store"]["doc-1"]["category"] == "procédure"
    assert len(monde["journaux"]["doc-1"][doc.ANALYSES_SUBCOLLECTION]) == 1
    # L'avertissement de présomption — c'est ce qui remplace le second clic
    # que la §5.3 exigeait.
    assert any("PRÉSUMÉE" in w for w in r["warnings"])

    import mcp.output_schemas as o
    import mcp.tools as t
    assert t.validate_args(o.OUTPUT_SCHEMAS["record_document_analysis"], r) == []


def test_a_broken_log_line_never_fails_a_committed_write(monde, monkeypatch):
    """Rien de ce qui SUIT un commit ne peut faire échouer l'écriture.

    `endpoint._tools_call` a un `except Exception` de dernier recours :
    une levée après le commit
    rapporte comme échouée une écriture commise, après quoi le modèle
    réessaie et ajoute une SECONDE entrée au journal. C'est le piège que le
    dépôt documente pour le bump de CTag — il vaut pour toute ligne posée
    après une écriture.
    """
    h = _handlers()
    monkeypatch.setattr(h.document_model, "get_document",
                        lambda i: dict(monde["store"].get(i) or {}) or None)
    monkeypatch.setattr(h.document_model, "record_analyse", doc.record_analyse)
    monkeypatch.setattr(h.dossier_model, "get_dossier", lambda i: None)

    import utils.logging_setup as ls

    def _explose(*a, **k):
        raise TypeError("journal cassé")

    monkeypatch.setattr(ls, "log_mcp_event", _explose)

    r = h.record_document_analysis(dict(_ARGS))
    assert r["recorded"] is True, "un journal cassé a fait échouer l'écriture"
    assert monde["store"]["doc-1"]["category"] == "procédure"


def test_the_audit_line_is_well_formed(monde, monkeypatch):
    """La garde protège l'écriture — elle ne doit pas cacher un appel cassé.

    Mesuré : une fois la garde en place, retirer l'argument `outcome` ne
    faisait plus rougir un seul test. La garde est bonne (l'écriture doit
    survivre), mais elle rend la ligne d'audit silencieusement cassable.
    Ce test l'inspecte directement, indépendamment de la garde — c'est lui
    qui aurait attrapé le bogue du 2026-08-27.
    """
    h = _handlers()
    monkeypatch.setattr(h.document_model, "get_document",
                        lambda i: dict(monde["store"].get(i) or {}) or None)
    monkeypatch.setattr(h.document_model, "record_analyse", doc.record_analyse)
    monkeypatch.setattr(h.dossier_model, "get_dossier", lambda i: None)

    import utils.logging_setup as ls
    vus = []
    vrai = ls.log_mcp_event

    def _espion(*a, **k):
        vus.append((a, k))
        return vrai(*a, **k)          # la VRAIE signature, donc un appel
                                      # malformé lève ici et se voit

    monkeypatch.setattr(ls, "log_mcp_event", _espion)
    h.record_document_analysis(dict(_ARGS))

    assert vus, "aucune ligne d'audit émise"
    args, kwargs = vus[0]
    assert args[0] == "mcp_document_analysed"
    assert args[1] == "success", "l'argument `outcome` est positionnel et requis"
    # Identifiants et codes seulement — le contenu est privilégié.
    for interdit in ("resume", "parties_mentionnees", "dispositif",
                     "indices_protection", "auteur"):
        assert interdit not in kwargs, interdit


# ── Le juriste garde la main (2026-08-27) ──────────────────────────────────

def test_a_manual_edit_reclaims_the_category(monde, monkeypatch):
    """Corriger la catégorie à la main est une DÉTERMINATION, pas une
    suggestion.

    `update_metadata` ne touchait pas `category_source` : une catégorie
    corrigée au formulaire restait donc marquée « analyse », donc
    « présumée » à l'écran et au connecteur — sur une valeur que le juriste
    venait de poser lui-même.
    """
    doc.record_analyse("doc-1", _SORTIE)
    assert monde["store"]["doc-1"]["category_source"] == "analyse"

    maj, err = doc.update_metadata("doc-1", {"category": "preuve"})
    assert err == []
    assert maj["category"] == "preuve"
    assert maj["category_source"] == "juriste"


def test_editing_another_field_leaves_the_source_alone(monde):
    """Renommer un document ne dit rien de sa catégorie."""
    doc.record_analyse("doc-1", _SORTIE)
    maj, err = doc.update_metadata("doc-1", {"display_name": "Autre nom"})
    assert err == []
    assert maj["category_source"] == "analyse", "un renommage a réclamé la catégorie"


# ── L'écran, après les retouches du 2026-08-27 ─────────────────────────────

def _rendu(**analyse):
    base = {**_DOC_RENDU["analyse"], **analyse}
    return _env().get_template("documents/_analyse.html").render(
        document={**_DOC_RENDU, "analyse": base}, analyses=[]
    )


def test_the_warnings_vanish_once_confirmed():
    """Confirmer, c'est dire « j'ai vu ». Les avertissements tombent; le
    journal, lui, ne s'efface jamais."""
    assert _rendu(confirme=False).count("bg-amber-50") == 3
    assert _rendu(confirme=True).count("bg-amber-50") == 0
    assert "Confirmer" not in _rendu(confirme=True)


def test_the_regime_label_is_never_printed_twice():
    """« Public » paraissait deux fois — une fois comme niveau, une fois
    comme code de privilège. La pastille vit dans l'en-tête; le bloc ne
    liste les régimes que lorsqu'ils sont CUMULÉS, ce que le niveau seul ne
    dit pas."""
    html = _rendu(privileges=["PUBLIC"], niveau_protection=0)
    assert "Régimes cumulés" not in html
    assert html.upper().count("PUBLIC") == 0

    cumul = _rendu(privileges=["LITIGE", "SECRET_PROFESSIONNEL"],
                   niveau_protection=3)
    assert "Régimes cumulés" in cumul


def test_the_block_is_mobile_first():
    """Une colonne par défaut, deux à partir de `sm`."""
    html = _rendu()
    import re
    # ⚠ Les variantes responsive ne sont PAS compilées dans l'artefact —
    # les 116 `sm:`/`md:`/`lg:` du dépôt sont inertes (mesuré 2026-08-27).
    # En poser une donnerait l'illusion d'un responsive qui n'existe pas.
    for g in re.findall(r'grid-cols-\d[^"]*', html):
        assert g.startswith("grid-cols-1"), g
    for prefixe in ('sm:', 'md:', 'lg:'):
        assert prefixe not in html, prefixe


# ── L'analyse alimente les champs natifs (2026-08-27) ──────────────────────

def test_the_analysis_fills_the_document_date(monde):
    """Ce qui la rend utile hors de sa propre carte : la date LUE devient
    la date du document, et le formulaire d'édition la montre alors, et la
    laisse corriger.

    Le résumé, lui, ne se recopie plus nulle part. Il alimentait
    `description` jusqu'au 2026-08-31 — donc après toute analyse les deux
    portaient la même chaîne, et depuis qu'elles s'éditaient séparément
    elles pouvaient en plus diverger. Le champ a été retiré : un document
    porte le texte du juriste (`notes_internes`) et celui du modèle
    (`analyse.resume`), et rien d'autre.
    """
    maj, err = doc.record_analyse("doc-1", _SORTIE)
    assert err == []
    assert maj["analyse"]["resume"] == _SORTIE["resume"]
    assert "description" not in maj
    assert maj["document_date"].strftime("%Y-%m-%d") == "2026-03-14"
    # Date-seule à minuit UTC (convention document_date).
    assert (maj["document_date"].hour, maj["document_date"].minute) == (0, 0)


def test_a_reanalysis_overwrites_and_journals_what_it_replaced():
    """L'analyse POSSÈDE `description` et `document_date`.

    Décision du praticien du 2026-08-27, renversant le remplir-si-vide de
    la veille. C'est tenable parce que `description` cesse d'être partagée
    — le juriste écrit dans `notes_internes`, que rien ne réécrit. Sans ce
    second champ, écrire une note et relancer une analyse s'excluaient.
    """
    pass  # remplacé ci-dessous par la version à harnais


def test_the_analysis_never_writes_a_third_text_field(monde):
    """Le champ retiré ne doit pas revenir par une couture oubliée.

    Une valeur héritée en base ne se recopie pas non plus : elle sera
    migrée hors ligne, et jusque-là elle n'a aucun effet.
    """
    monde["store"]["doc-1"]["description"] = "héritée"
    maj, _ = doc.record_analyse("doc-1", _SORTIE)
    assert maj["analyse"]["resume"] == _SORTIE["resume"]
    assert maj.get("description", "") == "héritée"   # intacte, jamais lue


# ── La page de détail rend TOUT (2026-08-27) ───────────────────────────────

def _detail(**over):
    """Rend le bloc `content` de detail.html, sans base.html."""
    from datetime import datetime, timezone
    e = _env()
    src = e.loader.get_source(e, "documents/detail.html")[0]
    i = src.index("{% block content %}") + len("{% block content %}")
    j = src.rindex("{% endblock %}")
    doc = {
        "id": "d1", "filename": "x.pdf", "display_name": "D.pdf",
        "file_type": "application/pdf", "category": "jugement",
        "category_source": "analyse", "_file_size_fmt": "1 Mo",
        "_file_icon": "pdf", "dossier_id": "dd",
        "dossier_file_number": "2026-034",
        "notes_internes": "Ma note.", "genere_depuis": "",
        "tags": ["portail"], "portail_invitation_id": "inv",
        "created_at": datetime(2026, 8, 26, tzinfo=timezone.utc),
        "analyse": {"sous_nature": "JUG_JUGEMENT", "nature_detectee": "jugement",
                    "famille": "JUDICIAIRE", "niveau_protection": 0,
                    "confirme": True},
    }
    doc.update(over)
    return e.from_string(src[i:j]).render(
        document=doc, signed_url="https://x/y.pdf", folder_breadcrumb=[],
        folder_tree=[], analyses=[], category_colors={"jugement": "bg-red-100"},
        category_labels={"jugement": "Jugement"}, return_to="",
    )


def test_the_detail_page_renders_every_section():
    """La PRÉSENCE, pas l'équilibre des balises.

    Vécu le 2026-08-27 : en remontant la carte d'aperçu, un script a mal
    borné le bloc et emporté la structure de la carte suivante. Le compte
    de `<div>` restait ÉQUILIBRÉ — 15/15 — et j'avais vérifié l'équilibre
    et l'ordre, jamais ce qui SORTAIT. Rendu, il ne restait que le
    visualiseur et le titre; le praticien l'a vu en production.
    """
    html = _detail()
    for nom, marqueur in (
        ("titre", "D.pdf"),
        ("catégorie", "Jugement"),
        ("dossier", "2026-034"),
        ("bloc d'analyse", ">Analyse<"),
        ("provenance du portail", "Reçu du portail"),
        # Le champ « description » est retiré depuis le 2026-08-31 : un
        # document porte le texte du JURISTE (notes internes) et celui du
        # MODÈLE (le résumé de l'analyse), pas un troisième qui recopiait
        # le second.
        ("notes internes", "Ma note."),
        ("étiquettes", ">portail<"),
        ("dates", "Ajouté le"),
        ("visualiseur", "<iframe"),
    ):
        assert marqueur in html, f"section absente du rendu : {nom}"


def test_a_pdf_offers_a_path_that_works_on_a_phone():
    """Un <iframe> ne rend PAS un PDF sur téléphone.

    Ni iOS Safari ni Chrome Android ne le font, et le cadre reste blanc —
    sans erreur, et sans repli : le contenu d'un `<iframe>` ne s'affiche
    que si l'ÉLÉMENT n'est pas supporté, ce qui n'arrive jamais. Le lien
    est donc le seul chemin qui marche, et il est visible PARTOUT — le
    cacher derrière une variante responsive ne marcherait pas non plus,
    puisque l'artefact compilé n'en contient aucune.
    """
    html = _detail()
    assert "Ouvrir le PDF" in html
    i_lien, i_frame = html.index("Ouvrir le PDF"), html.index("<iframe")
    assert i_lien < i_frame, "le lien doit précéder le cadre"
    for prefixe in ("sm:", "md:", "lg:"):
        bloc = html[i_lien - 400:i_frame]
        assert prefixe not in bloc, prefixe


def test_a_non_pdf_gets_no_pdf_link():
    html = _detail(file_type="image/png")
    assert "Ouvrir le PDF" not in html


def test_the_analysis_owns_the_document_date(monde):
    """Elle l'ÉCRASE, et le journal garde ce qu'elle a remplacé."""
    monde["store"]["doc-1"]["document_date"] = datetime(
        2020, 1, 1, tzinfo=timezone.utc
    )
    maj, err = doc.record_analyse("doc-1", _SORTIE)
    assert err == []
    assert maj["document_date"].strftime("%Y-%m-%d") == "2026-03-14"
    assert maj["analyse"]["date_document_precedente"].year == 2020


def test_the_analysis_never_touches_the_internal_notes(monde):
    """`notes_internes` est le champ du JURISTE. Que l'analyse ne puisse pas
    l'atteindre est ce qui rend l'écrasement de `description` supportable —
    et un test le vérifie sur le CODE, pas seulement sur un cas."""
    monde["store"]["doc-1"]["notes_internes"] = "Mon travail à moi."
    for _ in range(3):
        maj, err = doc.record_analyse("doc-1", _SORTIE)
        assert err == []
        assert maj["notes_internes"] == "Mon travail à moi."

    import inspect
    code = [
        l for l in inspect.getsource(doc.record_analyse).split(chr(10))
        if "notes_internes" in l and not l.strip().startswith("#")
    ]
    assert not code, f"l'analyse écrit dans les notes du juriste : {code}"


def test_the_internal_notes_are_editable(monde):
    maj, err = doc.update_metadata("doc-1", {"notes_internes": "À relire."})
    assert err == []
    assert maj["notes_internes"] == "À relire."


def test_the_edit_form_carries_the_internal_notes():
    """Un champ affiché mais que la route ne lit pas ne s'enregistre
    jamais — en silence, puisque le formulaire se soumet normalement."""
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent
    form = (racine / "templates" / "documents" / "edit.html").read_text(encoding="utf-8")
    route = (racine / "routes" / "documents.py").read_text(encoding="utf-8")
    detail = (racine / "templates" / "documents" / "detail.html").read_text(encoding="utf-8")

    assert 'name="notes_internes"' in form, "le formulaire ne porte pas le champ"
    assert 'f.get("notes_internes"' in route, "la route ne le lit pas"
    assert "document.notes_internes" in detail, "le détail ne l'affiche pas"

    import models.document as m
    assert "notes_internes" in m._default_doc()
