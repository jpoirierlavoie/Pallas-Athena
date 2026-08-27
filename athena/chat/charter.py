"""La charte système du clavardage (Phase N) — le SOCLE, et l'assemblage.

La charte est la constitution de l'outil : elle gouverne chaque tour de
chaque conversation. Jusqu'au 2026-08-27 elle vivait ENTIÈREMENT ici, sous
revue de code. Elle est désormais coupée en deux, sur décision du
praticien :

* le **SOCLE** reste ici, sous revue de code — l'identité, les devoirs
  épistémiques, les données privilégiées, et le fait qu'aucun outil ne
  supprime rien. Il est prépendé à toute version enregistrée ;
* le **corps** et l'**addendum planifié** vivent dans ``chat_charter``
  (``models/chat_charter.py``), versionnés append-only comme une skill, et
  s'éditent à l'écran.

Ce qu'on perd : la revue de code sur la moitié éditable. La charte était
l'un des TROIS seuls freins entre une exécution planifiée sans surveillance
et la parité d'écriture complète — les deux autres étant les clés
d'idempotence forcées et ``GATED_TOOLS``, vide en v1. Le socle est ce qui
rend la perte supportable : un lapsus à l'écran ne peut ni effacer une
règle déontologique, ni briquer le clavardage.

``BASE_CHARTER`` et ``SCHEDULED_ADDENDUM`` restent, **gelés au bit près** :
ils sont la version 1 — ce que porte tout tour antérieur au lot et tout
tour tombé en repli — et le PLANCHER sur lequel un tour retombe quand la
lecture Firestore échoue. Leur sha256 est épinglé : les éditer ferait
mentir le registre sur son passé.

Assemblage (``system_blocks``) : la charte résolue (+ addendum en exécution
planifiée, + le listing de ses fichiers) forme le premier bloc system, les
skills sélectionnées suivent en ordre STABLE (tri par identifiant —
l'ordre est porteur pour le cache de prompt), et le DERNIER bloc porte le
point d'arrêt ``cache_control`` — avec celui posé sur le dernier schéma
d'outil (chat/turn_engine.py), le préfixe stable tools → charte → skills
est intégralement couvert. Une sauvegarde de la charte invalide donc ce
préfixe pour toutes les conversations : coût assumé, quelques milliers de
jetons réécrits une fois par conversation.

Chaque tour enregistre ``charter_version`` à côté des paires ``(skill_id,
version)`` — le registre montre précisément quel texte gouvernait quelle
sortie. Le numéro seul ne suffit plus à le dire : ``charter_source`` sur le
tour distingue « Firestore » d'« amorçage » et de « repli ».
"""

from __future__ import annotations

from typing import Any, Optional

# « 1 » est la version du texte SOURCE — celui de ce fichier, servi en
# amorçage (aucune charte enregistrée) et en repli (lecture Firestore en
# échec). Il n'est plus gelé par sha : voir la note sur BASE_CHARTER.
SOURCE_CHARTER_VERSION: int = 1

# Le `skill_id` réservé sous lequel le modèle lit un fichier de référence de
# la CHARTE, via le même `get_skill_file`. Littéral tenu à la main plutôt
# qu'importé : ce module ne doit pas tirer `models/` (et donc le client
# Firestore) à l'import — le motif `_NOTE_CATEGORIES` de `mcp/tools.py`. La
# parité avec `models.chat_charter.DOC_ID` est épinglée par test.
CHARTER_FILE_ID: str = "charte"


# ── Le socle, la graine, et le texte de repli ──────────────────────────────
#
# Décision du praticien, 2026-08-27. Rendre la charte éditable retire la
# revue de code de l'un des TROIS seuls freins entre une exécution planifiée
# sans surveillance et la parité d'écriture complète (les deux autres : les
# clés d'idempotence forcées, et GATED_TOOLS, vide en v1). Le partage rend
# cette perte supportable :
#
#   SOCLE — sous revue de code, prépendé à toute version enregistrée. Ce qui
#   protège le client et n'a aucune raison de varier avec les méthodes de
#   travail : l'identité, les devoirs épistémiques, les données privilégiées,
#   et le fait qu'aucun outil ne supprime rien. Un lapsus à l'écran ne peut
#   donc ni effacer une règle déontologique, ni — puisque le socle est non
#   vide par construction — briquer le clavardage.
#
#   SEED_CORPS — ce que le formulaire prérémplit tant qu'aucune version
#   n'existe : les règles de sortie et la discipline d'écriture, c'est-à-dire
#   ce qu'on ajuste réellement à l'usage.

SOCLE: str = """\
Vous êtes l'assistant juridique interne du cabinet de Me Jason Poirier \
Lavoie, avocat au Barreau du Québec, plaideur devant les tribunaux civils \
de droit commun dans des affaires contentieuses (litiges civils et \
commerciaux). Votre unique interlocuteur est l'avocat lui-même.

DEVOIRS ÉPISTÉMIQUES
Vous n'inventez aucune citation ni aucun texte de loi, et ne présentez \
jamais une référence approximative comme exacte. La législation se lit par \
les outils legislation_*, la jurisprudence par les outils jurisprudence_* ; \
ce que ces outils n'ont pas confirmé n'est pas tenu pour établi. Toute \
citation jurisprudentielle figurant dans un brouillon ou une analyse doit \
avoir été passée à l'outil de vérification de citations pendant la \
conversation, avant livraison ; si les outils de vérification sont \
indisponibles, aucune citation n'est présentée comme vérifiée et chacune \
porte alors la mention « non vérifiée ». Toute incertitude est déclarée \
comme telle.

DONNÉES PRIVILÉGIÉES
Les pièces et documents des dossiers se lisent par les outils seulement — \
get_document_text et les outils de lecture du cabinet — et vous n'en citez \
que ce que la tâche exige. Aucun fait privilégié, aucun nom de client, \
aucun détail d'un dossier ne figure jamais dans une requête web_search : \
ces requêtes quittent l'infrastructure du cabinet. web_search sert à la \
doctrine et aux sources ouvertes ; tout ce que les systèmes du cabinet \
savent se demande aux outils internes d'abord. Enfin, la suppression \
n'existe pas : aucun outil ne supprime quoi que ce soit, et vous ne \
promettez jamais une suppression.
"""

SEED_CORPS: str = """\
RÈGLES DE SORTIE
- Vous répondez en français.
- Tout livrable est en markdown, et en markdown uniquement — jamais de \
HTML, jamais d'autre format.
- Les rédactions substantielles (projets de procédure, de lettre, \
d'analyse) vont dans un brouillon versionné via save_draft / revise_draft, \
pas dans le fil de la conversation.

DISCIPLINE D'ÉCRITURE
- Avant un geste conséquent ou ambigu (une écriture qui engage le dossier, \
une action difficile à défaire), ne l'exécutez pas : terminez votre tour \
par la question, et attendez la réponse de l'avocat.
- Proposez d'abord par dry_run: true — l'effet calculé est retourné sans \
que rien ne soit écrit — puis commettez sur instruction explicite, avec \
une idempotency_key.
"""

SCHEDULED_ADDENDUM: str = """\
EXÉCUTION PLANIFIÉE (SANS SURVEILLANCE)
- Cette exécution est déclenchée par une tâche planifiée : personne ne lit \
vos questions. N'en posez aucune ; ne terminez jamais votre tour en \
attente d'une réponse.
- Produisez un rapport markdown autonome et complet — c'est le livrable.
- Toute écriture conséquente se limite à une proposition : exécutez l'appel \
en dry_run: true et présentez l'effet calculé dans le rapport, pour que \
l'avocat commette lui-même. Les écritures de routine (notes, tâches, \
brouillons) portent obligatoirement une idempotency_key.
- Si un outil répond « refusé » parce qu'il exige une autorisation \
humaine, n'insistez pas : proposez l'action via dry_run dans le rapport.
"""

SEED_ADDENDUM: str = SCHEDULED_ADDENDUM

# ⚠ DÉRIVÉ, et plus gelé (2026-08-27, seconde décision du praticien).
#
# BASE_CHARTER est le texte de REPLI : ce qu'un tour reçoit quand aucune
# charte n'est enregistrée, ou quand la lecture Firestore échoue. Il était
# un littéral gelé par sha, pour que « charter_version: 1 » identifie des
# octets fixes. Il MIROITE désormais le socle et la graine, parce qu'un
# repli qui sert un texte périmé — tutoiement, anciennes règles — est un
# repli qui dégrade en silence sur le fond, pas seulement sur le numéro.
#
# Ce qu'on abandonne : « 1 » ne désigne plus des octets figés mais le texte
# source du commit déployé, résoluble par git. Le prix est mesuré — QUATRE
# tours du registre portent 1, tous antérieurs au lot — et la dérivation
# rend impossible la seule chose qui coûterait vraiment : un repli qui
# contredit la charte en vigueur.
BASE_CHARTER: str = SOCLE + "\n" + SEED_CORPS
# ── La charte RÉSOLUE ──────────────────────────────────────────────────────
#
# Une DONNÉE : ``{version, body, addendum, files}``, jamais un bloc
# prêt-à-servir. ``system_blocks`` fabrique toujours des blocs neufs, parce
# qu'il estampille ``cache_control`` sur le dernier — mémoïser un bloc
# partagé y poserait un second point d'arrêt, au mauvais endroit et sans
# erreur (le jumeau du piège que ``_build_tools`` documente déjà).


def source_charter() -> dict[str, Any]:
    """Le texte SOURCE, tel que tout tour estampillé 1 l'a vu."""
    return {
        "version": SOURCE_CHARTER_VERSION,
        "body": BASE_CHARTER,
        "addendum": SCHEDULED_ADDENDUM,
        "files": [],
    }


def charter_from_head(head: dict) -> dict[str, Any]:
    """Une tête ``chat_charter`` → la charte résolue : SOCLE + son corps."""
    corps = str(head.get("body") or "").strip()
    return {
        "version": int(head.get("current_version") or 0),
        "body": "\n\n".join(part for part in (SOCLE.strip(), corps) if part)
        + "\n",
        "addendum": str(head.get("addendum") or ""),
        "files": list(head.get("files") or []),
    }


def charter_text(
    charter: Optional[dict] = None, *, scheduled: bool = False
) -> str:
    """Le texte d'un tour — le corps, plus l'addendum en exécution planifiée.

    UN seul joint, explicite. Il y en a eu deux : la version source se
    concaténait BRUTE, parce que ``BASE_CHARTER`` était un littéral gelé
    par sha et que normaliser aurait changé ses octets. Il est DÉRIVÉ
    depuis le 2026-08-27, donc il n'y a plus d'octets à protéger — et un
    corps sans saut final collerait l'addendum à son dernier paragraphe,
    ce que le joint explicite empêche des deux côtés.
    """
    resolue = charter or source_charter()
    corps = str(resolue.get("body") or "")
    addendum = str(resolue.get("addendum") or "")
    if not scheduled or not addendum.strip():
        return corps
    return corps.rstrip() + "\n\n" + addendum.strip() + "\n"


def _file_listing(porteur: dict) -> str:
    """The progressive-disclosure listing appended INSIDE a carrier's block.

    A carrier is a skill — or the CHARTER, which grew the same reference
    files on 2026-08-27; it needs only ``{"id": …, "files": […]}``.

    Byte-stable by construction (the prompt-cache prefix depends on it):
    manifest order preserved as saved (never sorted here), plain ``str``
    ints, and the description segment OMITTED — not blank — when empty.
    A carrier without files returns "" and its block renders byte-identical
    to the pre-files format.

    ⚠ This FORMAT is charter.py CODE, not charter text: the version number
    does not move with it. For a skill that is benign. For the charter it
    means a code deploy can change the bytes of block 0 without moving
    ``charter_version`` — the one place the registre's promise is weaker
    than it looks. It is bounded on purpose: the format lives here, under
    code review, while what the listing NAMES lives in the version's own
    manifest, which does move with the number.
    """
    files = porteur.get("files") or []
    if not files:
        return ""
    lines = [
        "",
        "",
        "FICHIERS DE RÉFÉRENCE — à lire seulement au besoin, via l'outil "
        f"get_skill_file (skill_id : {str(porteur.get('id', ''))}) :",
    ]
    for entry in files:
        name = str(entry.get("name", ""))
        description = str(entry.get("description", "")).strip()
        chars = int(entry.get("chars") or 0)
        if description:
            lines.append(f"- {name} — {description} ({chars} caractères)")
        else:
            lines.append(f"- {name} ({chars} caractères)")
    return "\n".join(lines)


def system_blocks(
    skills: Optional[list[dict]] = None,
    *,
    scheduled: bool = False,
    charter: Optional[dict] = None,
) -> list[dict[str, Any]]:
    """Assemble the Messages API ``system`` array for a turn.

    ``skills`` entries are ``{"id": …, "name": …, "version": …, "body": …,
    "files": […]}`` (already resolved to the head version by the caller —
    the version used is recorded on the turn, SPEC §5). Ordering is by
    skill id, STABLE: the prompt-cache prefix depends on byte-identical
    assembly across the turn chain. The LAST block carries the
    ``cache_control`` breakpoint. A skill's reference files are LISTED
    inside its own block (never inlined) — the model reads them on demand
    through ``get_skill_file``.
    """
    resolue = charter or source_charter()
    texte = charter_text(resolue, scheduled=scheduled)
    if not texte.strip():
        # Un bloc texte VIDE fait répondre 400 à Vertex — donc chaque tour
        # de chaque conversation finalise `failed`, jusqu'à ré-édition. Le
        # bloc 0 ne pouvait pas être vide tant qu'il était une constante ;
        # il le peut depuis qu'il vient de Firestore. Les compétences à
        # corps blanc sont sautées depuis toujours (plus bas) ; la charte,
        # elle, ne peut pas être sautée — on retombe sur le texte source,
        # qui est un texte relu plutôt qu'une absence de charte.
        texte = charter_text(source_charter(), scheduled=scheduled)
    blocks: list[dict[str, Any]] = [{"type": "text", "text": texte}]
    blocks[0]["text"] += _file_listing(
        {"id": CHARTER_FILE_ID, "files": resolue.get("files") or []}
    )
    for skill in sorted(skills or [], key=lambda s: str(s.get("id", ""))):
        body = str(skill.get("body", "")).strip()
        if not body:
            continue
        name = str(skill.get("name", "")).strip()
        text = f"COMPÉTENCE — {name}\n\n{body}" if name else body
        blocks.append({"type": "text", "text": text + _file_listing(skill)})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks
