"""La moitié DÉTECTER du provisionneur — éprouvée sans gcloud ni réseau.

Trois propriétés portent ce fichier, et chacune répare une classe d'erreur
que ce dépôt a déjà payée ailleurs :

* **Un échec de sonde n'est JAMAIS une absence.** C'est la doctrine de
  ``get_coverage_report`` : un rapport ne fabrique pas un manquement à partir
  d'une lecture ratée. Sans elle, la toute première exécution réelle — où
  ``gcloud`` n'était pas résolvable depuis ``subprocess`` — aurait déclaré
  seize ressources absentes sur un projet entièrement provisionné.
* **Les colonnes se découpent sur la TABULATION, vides comprises.** Un
  ``str.split()`` nu décale tout ce qui suit une colonne vide ; mesuré le
  2026-09-13, cela a fait rapporter « accès uniforme DÉSACTIVÉ » sur un seau
  conforme. Un contrôle qui accuse à tort est pire qu'aucun contrôle.
* **Le corpus de production est REJOUÉ.** ``tests/fixtures/gcloud/
  production.json`` porte la sortie réelle de chaque sonde, caviardée de
  l'identifiant du propriétaire, enregistrée le 2026-09-13 contre un
  déploiement dont on sait qu'il est complet. Les dix verdicts doivent y lire
  ``present`` — c'est le seul test de ce fichier qui éprouve les GABARITS de
  commande et pas seulement la logique.
"""

import io
import json
import os
import sys

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

from utils.deployment_inventory import RESOURCES  # noqa: E402
from utils.deployment_probe import (  # noqa: E402
    ABSENT,
    DRIFT,
    INCONNU,
    MANUEL,
    PRESENT,
    Run,
    colonne,
    colonnes,
    exit_code,
    expected_index_count,
    gcloud_path,
    probe,
    probe_all,
    render_plan,
    Result,
)

_FIXTURE = os.path.join(_ATHENA, "tests", "fixtures", "gcloud",
                        "production.json")
TAB = chr(9)


def _corpus() -> dict:
    return json.load(io.open(_FIXTURE, encoding="utf-8"))


def _res(key: str):
    return next(r for r in RESOURCES if r.key == key)


def _runner(stdout: str = "", ok: bool = True, stderr: str = ""):
    """Un lanceur qui rend toujours la même chose. C'est ce seam qui rend
    presque tout ce module éprouvable sans identifiants."""
    def run(_argv):
        return Run(ok, stdout, stderr)
    return run


# ── La règle qui compte ──────────────────────────────────────────────────


def test_a_FAILED_probe_is_never_reported_as_an_absent_resource():
    """La première exécution réelle l'a prouvée utile avant même qu'on l'ait
    éprouvée : `subprocess` sur Windows n'ajoute que `.exe`, jamais `.cmd`,
    donc `gcloud` était injoignable — et le script a dit « rien n'a pu être
    constaté » sur dix ressources qui existaient toutes. Rapporter une absence
    aurait envoyé l'exploitant provisionner ce qui était déjà là."""
    for r in RESOURCES:
        if r.manual:
            continue
        for stderr in ("gcloud est introuvable", "PERMISSION_DENIED",
                       "credentials have expired", "délai dépassé (90 s)",
                       "has not been used in project", ""):
            res = probe(r, "p", "reg", runner=_runner(ok=False, stderr=stderr))
            assert res.state == INCONNU, (r.key, stderr, res.state)
            assert res.state != ABSENT


def test_a_probe_failure_names_a_REASON_the_operator_can_act_on():
    cas = {
        "gcloud est introuvable": "introuvable",
        "PERMISSION_DENIED: caller lacks permission": "permission",
        "Reauthentication required, please run gcloud auth login": "identifiants",
        "délai dépassé (90 s)": "délai",
        "Cloud Tasks API has not been used in project": "API",
    }
    r = _res("app-engine")
    for stderr, attendu in cas.items():
        res = probe(r, "p", "reg", runner=_runner(ok=False, stderr=stderr))
        assert attendu.lower() in res.detail.lower(), (stderr, res.detail)


def test_an_empty_stdout_on_a_SUCCESSFUL_probe_is_a_real_absence():
    """La règle précédente ne doit pas avaler le vrai cas : une commande qui
    réussit et ne rend rien dit bien que la ressource n'est pas là."""
    for cle in ("app-engine", "firestore-defaut", "seau-quarantaine",
                "file-portail", "sa-portail"):
        res = probe(_res(cle), "p", "reg", runner=_runner(""))
        assert res.state == ABSENT, (cle, res.state, res.detail)


# ── Le piège des colonnes ────────────────────────────────────────────────


def test_columns_split_on_TAB_and_keep_the_empty_ones():
    ligne = "A" + TAB + TAB + "C"
    assert colonnes(ligne) == ["A", "", "C"]
    assert colonne(colonnes(ligne), 1) == ""
    assert colonne(colonnes(ligne), 9) == ""
    # Le retour chariot de Windows ne doit pas coller à la dernière colonne.
    assert colonnes("A" + TAB + "B" + chr(13)) == ["A", "B"]


def test_an_empty_middle_column_does_not_shift_the_bucket_verdict():
    """LE défaut du 2026-09-13, épinglé. Le projet demandait
    `uniform_bucket_level_access.enabled`, champ qui n'existe pas ; gcloud
    rendait une colonne VIDE ; un `split()` nu lisait donc « enforced » comme
    la valeur de l'accès uniforme et criait au loup sur un seau conforme."""
    sortie = "NORTHAMERICA-NORTHEAST1" + TAB + TAB + "enforced"
    res = probe(_res("seau-quarantaine"), "p", "northamerica-northeast1",
                runner=_runner(sortie))
    # La colonne vide se lit comme « pas renseigné », donc on ne conclut RIEN
    # sur l'accès uniforme — et surtout pas l'inverse de ce qui est écrit.
    assert res.state == PRESENT, res.detail
    assert "DÉSACTIVÉ" not in res.detail


def test_the_bucket_projection_asks_for_a_field_that_EXISTS():
    """L'autre moitié du même incident : le gabarit lui-même était faux."""
    detect = " ".join(_res("seau-quarantaine").detect)
    assert "uniform_bucket_level_access," in detect or \
           detect.endswith("uniform_bucket_level_access")
    assert "uniform_bucket_level_access.enabled" not in detect


# ── Les verdicts, un par un ──────────────────────────────────────────────


def test_app_engine_region_is_authoritative_and_a_mismatch_is_drift():
    ctx = {"region_demandee": "europe-west1"}
    res = probe(_res("app-engine"), "p", "europe-west1",
                runner=_runner("northamerica-northeast1"), ctx=ctx)
    assert res.state == DRIFT
    assert "DÉFINITIVE" in res.detail
    # Et la région RÉELLE est retenue pour les ressources suivantes.
    assert ctx["region_reelle"] == "northamerica-northeast1"


def test_firestore_refuses_datastore_mode_and_a_foreign_region():
    ctx = {"region_reelle": "northamerica-northeast1"}
    r = _res("firestore-defaut")
    mauvais_mode = probe(r, "p", "", ctx=dict(ctx), runner=_runner(
        "northamerica-northeast1" + TAB + "DATASTORE_MODE"))
    assert mauvais_mode.state == DRIFT and "NATIVE" in mauvais_mode.detail
    ailleurs = probe(r, "p", "", ctx=dict(ctx), runner=_runner(
        "europe-west1" + TAB + "FIRESTORE_NATIVE"))
    assert ailleurs.state == DRIFT and "europe-west1" in ailleurs.detail


def test_the_index_count_is_DERIVED_from_the_versioned_file():
    attendu = expected_index_count()
    assert isinstance(attendu, int) and attendu > 0
    r = _res("index-composites")
    tous = probe(r, "p", "", ctx={"index_attendus": attendu},
                 runner=_runner("\n".join(["READY"] * attendu)))
    assert tous.state == PRESENT
    un_de_moins = probe(r, "p", "", ctx={"index_attendus": attendu},
                        runner=_runner("\n".join(["READY"] * (attendu - 1)
                                                 + ["CREATING"])))
    assert un_de_moins.state == DRIFT
    assert "LISTE VIDE" in un_de_moins.detail


def test_an_unreadable_index_file_yields_UNKNOWN_never_zero_expected():
    """« zéro index attendu » est une affirmation confiante et fausse — la
    forme du défaut « Page 7 / 0 »."""
    assert expected_index_count("/chemin/qui/nexiste/pas") is None
    res = probe(_res("index-composites"), "p", "",
                ctx={"index_attendus": None}, runner=_runner("READY"))
    assert res.state == INCONNU


def test_the_firewall_verdict_is_about_ORDER_not_presence():
    r = _res("pare-feu-interne")
    bon = (("10" + TAB + "ALLOW" + TAB + "0.1.0.2/32") + "\n"
           + ("100" + TAB + "ALLOW" + TAB + "173.245.48.0/20") + "\n"
           + ("2147483647" + TAB + "DENY" + TAB + "*"))
    assert probe(r, "p", "", runner=_runner(bon)).state == PRESENT

    # Autorisée, mais SOUS le refus par défaut : jamais atteinte.
    sous = (("100" + TAB + "DENY" + TAB + "*") + "\n"
            + ("500" + TAB + "ALLOW" + TAB + "0.1.0.2/32"))
    res = probe(r, "p", "", runner=_runner(sous))
    assert res.state == DRIFT and "SOUS le refus" in res.detail

    # Absente : le mode de défaillance silencieux que §7 enseignait.
    sans = (("100" + TAB + "ALLOW" + TAB + "173.245.48.0/20") + "\n"
            + ("2147483647" + TAB + "DENY" + TAB + "*"))
    res = probe(r, "p", "", runner=_runner(sans))
    assert res.state == DRIFT and "SILENCE" in res.detail

    # Aucun refus par défaut : l'origine est joignable en direct.
    ouvert = "10" + TAB + "ALLOW" + TAB + "0.1.0.2/32"
    res = probe(r, "p", "", runner=_runner(ouvert))
    assert res.state == DRIFT and "refus par défaut" in res.detail


def test_a_paused_queue_is_drift_and_says_why_it_looks_like_slowness():
    res = probe(_res("file-portail"), "p", "", runner=_runner("PAUSED"))
    assert res.state == DRIFT
    assert "quinze minutes" in res.detail


def test_the_lifecycle_verdict_compares_against_the_versioned_policy():
    r = _res("cycle-de-vie-quarantaine")
    bon = json.dumps({"lifecycle_config": {"rule": [
        {"action": {"type": "Delete"},
         "condition": {"age": 90, "matchesPrefix": ["submissions/"]}},
        {"action": {"type": "Delete"},
         "condition": {"age": 365, "matchesPrefix": ["archive/"]}},
    ]}})
    assert probe(r, "p", "", runner=_runner(bon)).state == PRESENT

    aucune = json.dumps({"lifecycle_config": {}})
    res = probe(r, "p", "", runner=_runner(aucune))
    assert res.state == ABSENT and "POUR TOUJOURS" in res.detail

    trop_court = bon.replace('"age": 365', '"age": 7')
    res = probe(r, "p", "", runner=_runner(trop_court))
    assert res.state == DRIFT and "archive/" in res.detail


def test_a_manual_step_is_never_probed():
    """Une étape de console n'a pas de commande ; l'inventer serait prétendre
    la vérifier."""
    appels = []

    def espion(argv):
        appels.append(argv)
        return Run(True, "peu importe")

    for r in RESOURCES:
        if not r.manual:
            continue
        assert probe(r, "p", "reg", runner=espion).state == MANUEL
    assert appels == []


# ── Le plan, le code de sortie, et le corpus ─────────────────────────────


def test_exit_code_separates_BROKEN_from_NOT_DONE_YET():
    """Un CI qui confond « va cliquer dans une console » avec « quelque chose
    est cassé » finit par ignorer les deux."""
    assert exit_code([Result("a", PRESENT)]) == 0
    assert exit_code([Result("a", PRESENT), Result("b", MANUEL)]) == 2
    assert exit_code([Result("a", PRESENT), Result("b", ABSENT)]) == 2
    assert exit_code([Result("a", PRESENT), Result("b", INCONNU)]) == 2
    # La dérive l'emporte : elle dit qu'une ressource CONTREDIT le code.
    assert exit_code([Result("a", ABSENT), Result("b", DRIFT)]) == 1


def test_the_plan_lists_only_what_remains():
    resultats = [Result(r.key, PRESENT) for r in RESOURCES]
    assert "rien à faire" in render_plan(resultats)
    partiel = [Result(r.key, PRESENT if r.key != "file-portail" else ABSENT)
               for r in RESOURCES]
    texte = render_plan(partiel)
    assert "file-portail" in texte
    assert "app-engine" not in texte


def test_the_plan_marks_the_irreversible_steps():
    resultats = [Result(r.key, ABSENT if not r.manual else MANUEL)
                 for r in RESOURCES]
    texte = render_plan(resultats)
    for r in RESOURCES:
        if r.irreversible:
            i = texte.index(r.key)
            assert "DÉFINITIF" in texte[i:i + 160], r.key


def test_the_recorded_production_corpus_replays_to_PRESENT():
    """Le seul test qui éprouve les GABARITS et pas seulement la logique.

    Le corpus est la sortie RÉELLE de chaque sonde, enregistrée le 2026-09-13
    contre un déploiement dont on sait qu'il est complet, et caviardée de
    l'identifiant du propriétaire. S'il cesse de rendre `present`, soit un
    verdict a dérivé, soit un gabarit de commande demande un champ que gcloud
    n'expose pas — le défaut exact que ce corpus existe pour attraper.
    """
    corpus = _corpus()

    def rejoueur(argv):
        # La clé se retrouve par la ressource dont le gabarit a produit cet
        # argv : on compare sur le projet témoin du corpus.
        for r in RESOURCES:
            if r.manual:
                continue
            if r.argv("projet-temoin", "northamerica-northeast1") == tuple(argv):
                e = corpus[r.key]
                return Run(e["ok"], e["stdout"])
        raise AssertionError("argv hors corpus : " + repr(argv))

    resultats = probe_all("projet-temoin", "northamerica-northeast1",
                          runner=rejoueur)
    par_cle = {r.key: r for r in resultats}
    for r in RESOURCES:
        attendu = MANUEL if r.manual else PRESENT
        assert par_cle[r.key].state == attendu, (
            r.key, par_cle[r.key].state, par_cle[r.key].detail
        )


def test_the_corpus_carries_no_owner_literal_and_no_payload():
    """Un corpus de test est un fichier versionné. Celui-ci vient d'un
    déploiement vivant, donc il se caviarde — et aucune commande de ce module
    ne lisant de secret, il n'a rien d'autre à cacher."""
    from utils.deployment_inventory import OWNER_LITERALS

    brut = io.open(_FIXTURE, encoding="utf-8").read()
    for cle, valeur in OWNER_LITERALS.items():
        if cle == "App Engine region":
            continue  # la région n'est pas un identifiant, et elle est LUE
        assert valeur not in brut, (cle, "littéral du propriétaire au corpus")
    for mot in ("BEGIN ", "PRIVATE", "password", "$2b$"):
        assert mot.lower() not in brut.lower(), mot


def test_gcloud_is_resolved_to_a_full_path_outside_the_working_directory():
    """Ce module EXÉCUTE ce qu'il résout. Accepter une résolution dans le
    répertoire courant reviendrait à laisser un dépôt cloné fournir son
    propre « gcloud »."""
    chemin, motif = gcloud_path()
    assert chemin or motif, "ni chemin ni motif — le silence n'est pas un état"
    if chemin:
        assert os.path.isabs(chemin) or os.environ.get("GCLOUD_BIN")
        assert os.path.dirname(os.path.abspath(chemin)) != \
            os.path.abspath(os.getcwd())


def test_no_web_surface_may_import_the_probe():
    """La sonde lance des PROCESSUS. Une route Flask qui l'importerait
    mettrait un `subprocess` de quatre-vingt-dix secondes sur le chemin d'une
    requête servie par deux travailleurs et quatre fils — et `gcloud` n'existe
    de toute façon pas sur App Engine. La séparation est donc structurelle :
    le rapport partagé vit dans `config_checks`, que la page consomme ; la
    sonde reste un outil de terminal.
    """
    import ast

    interdits = []
    for dossier in ("routes", "utils", "models", "mcp", "client"):
        racine = os.path.join(_ATHENA, dossier)
        for base, _d, fichiers in os.walk(racine):
            if "__pycache__" in base:
                continue
            for nom in fichiers:
                if not nom.endswith(".py") or nom == "deployment_probe.py":
                    continue
                chemin = os.path.join(base, nom)
                arbre = ast.parse(io.open(chemin, encoding="utf-8").read())
                for n in ast.walk(arbre):
                    cible = ""
                    if isinstance(n, ast.ImportFrom):
                        cible = n.module or ""
                    elif isinstance(n, ast.Import):
                        cible = ",".join(a.name for a in n.names)
                    if "deployment_probe" in cible:
                        interdits.append(os.path.relpath(chemin, _ATHENA))
    assert not interdits, (
        "ces modules importent la sonde, qui lance des processus : "
        + repr(sorted(set(interdits)))
    )
