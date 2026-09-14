"""Constater ce qui est provisionné — et rien d'autre.

La moitié DÉTECTER d'un provisionneur. Il n'y a pas d'``apply()`` ici, et
l'absence est **structurelle** plutôt que promise : ``deployment_inventory``
n'expose que des verbes de lecture, et un balayage de
``tests/test_deployment_inventory.py`` refuse par liste blanche tout verbe qui
n'est pas ``describe`` / ``list`` / ``get-iam-policy``. La raison est la leçon
du 2026-09-11, payée quatre fois dans une seule journée : une commande écrite
mais jamais exécutée est confiante et fausse, et la moitié qui MUTE ne peut pas
être éprouvée sans dépenser un projet jetable.

Trois règles portent ce module, et chacune répare une classe d'erreur que ce
dépôt a déjà commise ailleurs :

* **Un échec de sonde n'est JAMAIS une absence.** ``gcloud`` introuvable, des
  identifiants périmés, une API désactivée — tout cela rend ``INCONNU``, jamais
  ``ABSENT``. C'est la doctrine de ``get_coverage_report`` : un rapport ne doit
  pas fabriquer un manquement à partir d'une lecture ratée, sans quoi on envoie
  l'exploitant provisionner ce qui existe déjà.
* **Le projet est toujours EXPLICITE.** Aucune commande n'hérite de
  ``gcloud config`` : un projet actif périmé est la façon dont on inspecte —
  ou provisionne — chez quelqu'un d'autre.
* **Aucune charge ne transite par un argument.** Ce module ne lit aucun secret
  et n'en écrit aucun ; les seules valeurs qu'il manipule sont des noms, des
  régions et des états.

``runner`` est injectable, ce qui est ce qui rend la logique de verdict
testable sans ``gcloud``, sans identifiants et sans réseau — soit la majeure
partie de ce fichier.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

from utils.deployment_inventory import (
    APPENGINE_INTERNAL_CIDR,
    PHASE_ORDER,
    QUARANTINE_LIFECYCLE,
    RESOURCES,
    Resource,
    provisioning_plan,
)
from utils.deployment_report import FAIL, OK, WARN, Report

SECTION_RESOURCES = "5. Provisioned resources"

PRESENT = "present"
ABSENT = "absent"
DRIFT = "drift"
INCONNU = "inconnu"
MANUEL = "manuel"

# Le délai d'une sonde. Généreux — `gcloud` démarre un interpréteur Python —
# mais BORNÉ : un provisionneur qui pend sur une commande est indiscernable
# d'un provisionneur en panne.
_TIMEOUT_S = 90


@dataclass(frozen=True)
class Result:
    """Le verdict d'UNE ressource."""

    key: str
    state: str
    detail: str = ""
    #: ce que la commande a réellement rendu, tronqué — jamais un secret,
    #: puisque aucune commande de ce module n'en lit un.
    observed: str = ""


@dataclass(frozen=True)
class Run:
    """Ce qu'un lanceur rend. Séparé de ``Result`` à dessein : l'un est un
    fait sur un processus, l'autre un jugement sur une ressource."""

    ok: bool
    stdout: str = ""
    stderr: str = ""


def gcloud_path() -> tuple:
    """Où est `gcloud`, et pourquoi on ne le trouve pas. Rend (chemin, motif).

    ⚠ Mesuré le 2026-09-13, et c'est le genre de défaut qui ne se voit qu'en
    exécutant : sur Windows, `subprocess` avec `shell=False` appelle
    `CreateProcess`, qui n'ajoute QUE `.exe` — jamais `.cmd`. Or le lanceur
    installé par le SDK est `gcloud.CMD`. Passer la chaîne « gcloud » lève
    donc `FileNotFoundError` alors que le SDK est là, authentifié, et que le
    même mot marche dans n'importe quel shell. On résout donc le chemin
    COMPLET avant d'exécuter, ce qui règle aussi l'ambiguïté de PATH.

    ⚠ Et on REFUSE une résolution dans le répertoire courant : sur Windows,
    `shutil.which` y regarde d'abord. Ce module exécute ce qu'il résout, donc
    accepter le répertoire courant reviendrait à laisser un dépôt cloné
    fournir son propre « gcloud ».
    """
    impose = os.environ.get("GCLOUD_BIN", "").strip()
    if impose:
        if os.path.isfile(impose):
            return impose, ""
        return "", "GCLOUD_BIN désigne « %s », qui n'est pas un fichier" % impose
    trouve = shutil.which("gcloud")
    if not trouve:
        return "", ("gcloud est introuvable — sur Windows, le lanceur du SDK "
                    "s'appelle gcloud.CMD ; posez GCLOUD_BIN si besoin")
    if os.path.dirname(os.path.abspath(trouve)) == os.path.abspath(os.getcwd()):
        return "", ("gcloud a été résolu DANS le répertoire courant — refusé. "
                    "Posez GCLOUD_BIN sur le vrai lanceur du SDK.")
    return trouve, ""


def _run(argv: tuple) -> Run:
    """Le lanceur réel. Le seul endroit de ce module qui touche l'extérieur."""
    chemin, motif = gcloud_path()
    if not chemin:
        return Run(False, stderr=motif)
    argv = (chemin,) + tuple(argv[1:])
    try:
        p = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_S,
            shell=False,
        )
    except FileNotFoundError:
        return Run(False, stderr="gcloud introuvable : " + chemin)
    except subprocess.TimeoutExpired:
        return Run(False, stderr="délai dépassé (%d s)" % _TIMEOUT_S)
    except OSError as exc:  # pragma: no cover - dépend de l'OS
        return Run(False, stderr=type(exc).__name__)
    return Run(p.returncode == 0, (p.stdout or "").strip(),
               (p.stderr or "").strip())


# ── Lire ce qui est ATTENDU, plutôt que de le recopier ───────────────────

_ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_ATHENA_DIR)


def expected_index_count(repo_root: str = "") -> Optional[int]:
    """Combien d'index composites `firestore.indexes.json` déclare.

    DÉRIVÉ du fichier versionné, jamais recopié : c'est ce qui fait qu'ajouter
    un index et oublier de le déployer se voit. Rend ``None`` — jamais 0 — si
    le fichier est illisible : « zéro index attendu » est une affirmation
    confiante et fausse, la forme du défaut « Page 7 / 0 ».
    """
    chemin = os.path.join(repo_root or _REPO_ROOT, "firestore.indexes.json")
    try:
        with open(chemin, encoding="utf-8") as fh:
            return len(json.load(fh).get("indexes", []))
    except Exception:
        return None


# ⚠ `gcloud --format=value(a,b,c)` sépare par des TABULATIONS et laisse la
# colonne d'un champ absent VIDE. Un `str.split()` nu réduit les blancs et
# DÉCALE donc tout ce qui suit d'un cran — mesuré le 2026-09-13 : le seau de
# quarantaine s'est fait rapporter « accès uniforme DÉSACTIVÉ » alors que la
# valeur lue était « enforced », la colonne de l'accès public glissée d'une
# place. Un contrôle qui accuse à tort est pire qu'aucun contrôle : on le
# désarme, et il ne dit plus rien le jour où il a raison.
#
# Deux règles en sortent, et elles vont ensemble : on découpe sur la
# TABULATION en gardant les vides, et le champ demandé doit EXISTER — le même
# incident venait pour moitié d'avoir projeté
# `uniform_bucket_level_access.enabled` là où gcloud expose
# `uniform_bucket_level_access`, un booléen nu.


def colonnes(ligne: str) -> list:
    """Les colonnes d'une ligne `value(...)`, vides comprises."""
    return [c.strip() for c in ligne.replace(chr(13), "").split(chr(9))]


def colonne(champs: list, i: int) -> str:
    """La i-ème colonne, ou la chaîne vide. Jamais d'IndexError : une colonne
    manquante est un fait à rapporter, pas une exception à lever."""
    return champs[i] if i < len(champs) else ""


# ── Les verdicts, un par ressource ───────────────────────────────────────
#
# Chacun reçoit la sortie BRUTE et rend (état, détail). Ils sont PURS :
# aucun n'appelle `gcloud`, ce qui les rend testables sans identifiants et
# sans réseau. C'est là que vivent presque toutes les décisions.


def _v_app_engine(run: Run, ctx: dict) -> tuple:
    region = run.stdout.strip()
    if not region:
        return ABSENT, "aucune application App Engine"
    ctx["region_reelle"] = region
    if ctx.get("region_demandee") and region != ctx["region_demandee"]:
        return DRIFT, (
            "l'application est en « %s » alors que l'inspection demandait "
            "« %s ». La région est DÉFINITIVE : c'est celle de l'application "
            "qui fait autorité, et tout le reste doit s'y conformer."
            % (region, ctx["region_demandee"])
        )
    return PRESENT, "région " + region


def _v_firestore(run: Run, ctx: dict) -> tuple:
    champs = colonnes(run.stdout)
    location, type_ = colonne(champs, 0), colonne(champs, 1)
    if not location:
        return ABSENT, "base absente"
    if type_ and type_ != "FIRESTORE_NATIVE":
        return DRIFT, "mode « %s » — il faut FIRESTORE_NATIVE" % type_
    attendue = ctx.get("region_reelle") or ctx.get("region_demandee")
    if attendue and location.lower() != attendue.lower():
        return DRIFT, (
            "en « %s » alors que l'application App Engine est en « %s »"
            % (location, attendue)
        )
    return PRESENT, location + " / " + (type_ or "type inconnu")


def _v_index(run: Run, ctx: dict) -> tuple:
    etats = [l.strip() for l in run.stdout.splitlines() if l.strip()]
    prets = sum(1 for e in etats if e.upper() == "READY")
    attendu = ctx.get("index_attendus")
    if attendu is None:
        return INCONNU, (
            "firestore.indexes.json est illisible — impossible de dire "
            "combien d'index sont attendus"
        )
    if not etats:
        return ABSENT, "aucun index composite (%d attendus)" % attendu
    if prets < attendu:
        return DRIFT, (
            "%d index READY sur %d déclarés%s. Tant qu'un index construit, la "
            "requête qu'il sert se dégrade en LISTE VIDE, jamais en erreur."
            % (prets, attendu,
               " (%d en construction)" % (len(etats) - prets)
               if len(etats) > prets else "")
        )
    return PRESENT, "%d index READY" % prets


def _v_ttl(run: Run, ctx: dict) -> tuple:
    champs = [c for c in colonnes(run.stdout.strip()) if c]
    if not champs:
        return ABSENT, "aucune politique TTL sur oauth_codes.expire_at"
    if champs[-1].upper() != "ACTIVE":
        return DRIFT, "état « %s »" % champs[-1]
    return PRESENT, "ACTIVE"


def _v_existe(run: Run, ctx: dict) -> tuple:
    if not run.stdout.strip():
        return ABSENT, "introuvable"
    return PRESENT, run.stdout.strip().splitlines()[0][:120]


def _v_seau(run: Run, ctx: dict) -> tuple:
    champs = colonnes(run.stdout)
    location = colonne(champs, 0)
    ubla = colonne(champs, 1)
    public = colonne(champs, 2)
    if not location:
        return ABSENT, "seau absent"
    griefs = []
    attendue = ctx.get("region_reelle") or ctx.get("region_demandee")
    if attendue and location.lower() != attendue.lower():
        griefs.append(
            "en « %s » alors que l'application est en « %s » — chaque octet "
            "d'un versement paierait alors de l'egress inter-régional"
            % (location, attendue)
        )
    if ubla and ubla.lower() not in ("true", "yes"):
        griefs.append("accès uniforme DÉSACTIVÉ")
    if public and public.lower() != "enforced":
        griefs.append("prévention de l'accès public « %s »" % public)
    if griefs:
        return DRIFT, " ; ".join(griefs)
    return PRESENT, location


def _v_cycle_de_vie(run: Run, ctx: dict) -> tuple:
    try:
        charge = json.loads(run.stdout or "{}")
    except ValueError:
        return INCONNU, "sortie JSON illisible"
    regles = ((charge.get("lifecycle_config") or charge.get("lifecycle") or {})
              .get("rule") or [])
    vus = {}
    for r in regles:
        cond = r.get("condition") or {}
        age = cond.get("age")
        for prefixe in cond.get("matchesPrefix") or []:
            if (r.get("action") or {}).get("type") == "Delete":
                vus[prefixe] = age
    if not vus:
        return ABSENT, (
            "aucune règle de cycle de vie — le matériel privilégié d'un "
            "client resterait en quarantaine POUR TOUJOURS"
        )
    griefs = []
    for regle in QUARANTINE_LIFECYCLE:
        p, attendu = regle["prefix"], regle["age_days"]
        if p not in vus:
            griefs.append("« %s » n'a aucune règle" % p)
        elif vus[p] != attendu:
            griefs.append("« %s » à %s jours au lieu de %d"
                          % (p, vus[p], attendu))
    if griefs:
        return DRIFT, " ; ".join(griefs)
    return PRESENT, ", ".join("%s %s j" % (p, a) for p, a in sorted(vus.items()))


def _v_file(run: Run, ctx: dict) -> tuple:
    champs = colonnes(run.stdout)
    etat = colonne(champs, 0)
    if not etat:
        return ABSENT, "file absente"
    if etat.upper() == "PAUSED":
        return DRIFT, (
            "la file est EN PAUSE — les tâches s'accumulent sans être "
            "livrées, et le cron de réconciliation les réenfile toutes les "
            "quinze minutes sans jamais les faire passer"
        )
    if etat.upper() != "RUNNING":
        return DRIFT, "état « %s »" % etat
    return PRESENT, "RUNNING " + " ".join(c for c in champs[1:] if c)


def _v_pare_feu(run: Run, ctx: dict) -> tuple:
    """L'ORDRE est le fond du sujet, pas la présence.

    Une règle ALLOW sur l'adresse interne qui se trouve SOUS le refus par
    défaut ne sert à rien ; et sans elle, les trois tâches cron et toute la
    file sont coupées à la couche 1, en silence.
    """
    lignes = [colonnes(l) for l in run.stdout.splitlines() if l.strip()]
    if not lignes:
        return ABSENT, "aucune règle de pare-feu"
    interne = None
    refus_par_defaut = None
    for champs in lignes:
        if len(champs) < 3:
            continue
        try:
            priorite = int(champs[0])
        except ValueError:
            continue
        action, plage = champs[1].upper(), champs[2]
        if plage == APPENGINE_INTERNAL_CIDR and action == "ALLOW":
            interne = priorite
        if plage == "*" and action == "DENY":
            refus_par_defaut = priorite
    if interne is None:
        return DRIFT, (
            "%s n'est pas autorisé — les trois tâches cron et la file Cloud "
            "Tasks sont coupées à la couche 1, EN SILENCE. App Engine les "
            "expédie depuis cette adresse interne, qui n'est pas une plage "
            "Cloudflare." % APPENGINE_INTERNAL_CIDR
        )
    if refus_par_defaut is not None and interne >= refus_par_defaut:
        return DRIFT, (
            "%s est autorisé en priorité %d, SOUS le refus par défaut (%d) — "
            "donc jamais atteint"
            % (APPENGINE_INTERNAL_CIDR, interne, refus_par_defaut)
        )
    if refus_par_defaut is None:
        return DRIFT, (
            "aucun refus par défaut : l'origine est joignable directement, "
            "et le secret d'origine devient la seule couche restante"
        )
    return PRESENT, "%s en priorité %d, %d règles en tout" % (
        APPENGINE_INTERNAL_CIDR, interne, len(lignes))


_VERDICTS: dict = {
    "app-engine": _v_app_engine,
    "firestore-defaut": _v_firestore,
    "firestore-portail": _v_firestore,
    "index-composites": _v_index,
    "ttl-firestore": _v_ttl,
    "sa-portail": _v_existe,
    "seau-quarantaine": _v_seau,
    "cycle-de-vie-quarantaine": _v_cycle_de_vie,
    "file-portail": _v_file,
    "pare-feu-interne": _v_pare_feu,
}


def probe(resource: Resource, project: str, region: str, *,
          runner: Optional[Callable] = None,
          ctx: Optional[dict] = None) -> Result:
    """Constater UNE ressource. Ne mute jamais rien."""
    if resource.manual:
        return Result(resource.key, MANUEL, resource.expect)
    verdict = _VERDICTS.get(resource.key)
    if verdict is None:
        return Result(resource.key, INCONNU,
                      "aucun verdict n'est écrit pour cette ressource")
    run = (runner or _run)(resource.argv(project, region))
    if not run.ok:
        # ⚠ JAMAIS `ABSENT`. Une sonde qui échoue ne prouve rien sur la
        # ressource, et rapporter une absence enverrait l'exploitant
        # provisionner ce qui existe peut-être déjà.
        return Result(resource.key, INCONNU,
                      _motif_de_sonde(run.stderr), run.stderr[:200])
    etat, detail = verdict(run, ctx if ctx is not None else {})
    return Result(resource.key, etat, detail, run.stdout[:200])


def _motif_de_sonde(stderr: str) -> str:
    bas = (stderr or "").lower()
    if "introuvable" in bas or "refusé" in bas or "gcloud_bin" in bas:
        return stderr or "gcloud est introuvable — rien n'a pu être constaté"
    if "délai dépassé" in bas or "timeout" in bas:
        return "la commande a dépassé son délai — rien n'a pu être constaté"
    if "credential" in bas or "login" in bas or "unauthenticated" in bas:
        return ("identifiants absents ou périmés (`gcloud auth login`) — "
                "rien n'a pu être constaté")
    if "permission" in bas or "denied" in bas or "403" in bas:
        return ("permission refusée — la ressource existe peut-être ; "
                "l'inspection, elle, n'a pas eu lieu")
    if "has not been used" in bas or "disabled" in bas or "service_disabled" in bas:
        return "l'API nécessaire est désactivée — rien n'a pu être constaté"
    return "la commande a échoué — rien n'a pu être constaté"


def probe_all(project: str, region: str = "", *,
              runner: Optional[Callable] = None,
              repo_root: str = "") -> list:
    """Toutes les ressources, dans l'ordre des phases.

    Le contexte se remplit CHEMIN FAISANT : la région réelle est lue de
    l'application App Engine, puis c'est ELLE qui sert de référence aux bases,
    au seau et à la file — jamais celle passée en argument. La région d'une
    application est définitive ; c'est donc elle qui fait autorité, et pas ce
    que l'exploitant a tapé.
    """
    ctx = {
        "region_demandee": region,
        "index_attendus": expected_index_count(repo_root),
    }
    resultats = []
    for r in provisioning_plan():
        res = probe(r, project, region or ctx.get("region_reelle", ""),
                    runner=runner, ctx=ctx)
        resultats.append(res)
    return resultats


# ── Le rendu, dans la grammaire de rapport existante ─────────────────────

_NIVEAU = {
    PRESENT: OK,
    # Une ressource pas encore posée n'est pas une PANNE : c'est du travail
    # qui reste. Le code de sortie 2 la distingue d'un échec.
    ABSENT: WARN,
    # Elle existe et contredit ce que le code attend — ça, c'est un défaut.
    DRIFT: FAIL,
    INCONNU: WARN,
    MANUEL: WARN,
}


def check_resources(rpt: Report, project: str, region: str = "", *,
                    runner: Optional[Callable] = None,
                    repo_root: str = "") -> list:
    """Émettre les constats dans le rapport partagé."""
    rpt.section(SECTION_RESOURCES)
    resultats = probe_all(project, region, runner=runner, repo_root=repo_root)
    par_cle = {r.key: r for r in RESOURCES}
    for res in resultats:
        ressource = par_cle[res.key]
        if res.state == MANUEL:
            rpt.emit(WARN, "%s : à vérifier à la main — %s"
                     % (ressource.label, ressource.expect),
                     resource=res.key, state=res.state, phase=ressource.phase)
            continue
        message = "%s : %s" % (ressource.label, res.detail or res.state)
        if res.state in (ABSENT, DRIFT):
            message += " — " + ressource.symptom
        rpt.emit(_NIVEAU[res.state], message,
                 resource=res.key, state=res.state, phase=ressource.phase)
    return resultats


def render_plan(resultats: list) -> str:
    """Ce qui RESTE à faire, dans l'ordre — c'est le livrable.

    Cinq des huit défaillances connues d'un clone neuf sont des défaillances
    d'ORDRE, pas de commande ; c'est pourquoi le plan groupe par phase au lieu
    de rendre une liste à plat.
    """
    par_cle = {r.key: r for r in RESOURCES}
    restants = [r for r in resultats
                if r.state in (ABSENT, DRIFT, INCONNU, MANUEL)]
    if not restants:
        return "Plan : rien à faire — chaque ressource constatable est en place."
    lignes = ["Plan de provisionnement — ce qui reste, dans l'ordre", ""]
    for phase in PHASE_ORDER:
        du_lot = [r for r in restants if par_cle[r.key].phase == phase]
        if not du_lot:
            continue
        lignes.append("  [%s]" % phase)
        for res in du_lot:
            ressource = par_cle[res.key]
            marque = {
                ABSENT: "à poser  ", DRIFT: "À CORRIGER", INCONNU: "?        ",
                MANUEL: "à la main",
            }[res.state]
            irr = "  ⚠ DÉFINITIF" if ressource.irreversible else ""
            lignes.append("    %s  %-26s %s%s"
                          % (marque, ressource.key, ressource.label, irr))
            if res.detail:
                lignes.append("                 %s" % res.detail)
        lignes.append("")
    return "\n".join(lignes).rstrip()


def exit_code(resultats: list) -> int:
    """0 tout est en place · 1 une dérive · 2 du travail reste.

    Le 2 distingué compte : «  va cliquer dans une console » n'est pas « quelque
    chose est cassé », et un CI qui les confond finit par ignorer les deux.
    """
    etats = {r.state for r in resultats}
    if DRIFT in etats:
        return 1
    if ABSENT in etats or INCONNU in etats or MANUEL in etats:
        return 2
    return 0


__all__ = [
    "ABSENT",
    "DRIFT",
    "INCONNU",
    "MANUEL",
    "PRESENT",
    "Result",
    "Run",
    "SECTION_RESOURCES",
    "check_resources",
    "colonne",
    "colonnes",
    "exit_code",
    "expected_index_count",
    "gcloud_path",
    "probe",
    "probe_all",
    "render_plan",
]
