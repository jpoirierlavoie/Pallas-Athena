"""La charte système du clavardage (Phase N) — versionnée SOUS CONTRÔLE DE
SOURCE, jamais éditable à chaud.

« Versionnée en config » se lit ainsi : la charte est la constitution de
l'outil — un texte français de plusieurs paragraphes qui doit être relu en
revue de code, pas une valeur d'environnement ni un document Firestore. La
couche chaude, éditable à l'exécution, ce sont les *skills*
(``chat_skills``) ; la charte, elle, ne change que par commit, et chaque
tour enregistre ``charter_version`` à côté des paires ``(skill_id,
version)`` — le registre montre précisément quel texte gouvernait quelle
sortie.

Toute modification du texte incrémente ``CHARTER_VERSION``. Le texte de
base et l'addendum ont été rédigés par Claude Code et soumis à
l'approbation de Me Poirier Lavoie avec le lot qui les porte (plan Phase N,
2026-08-26).

Assemblage (``system_blocks``) : la charte (+ addendum en exécution
planifiée) forme le premier bloc system, les skills sélectionnées suivent
en ordre STABLE (tri par identifiant — l'ordre est porteur pour le cache de
prompt), et le DERNIER bloc porte le point d'arrêt ``cache_control`` — avec
celui posé sur le dernier schéma d'outil (chat/vertex.py), le préfixe
stable tools → charte → skills est intégralement couvert.
"""

from __future__ import annotations

from typing import Any, Optional

CHARTER_VERSION: int = 1

BASE_CHARTER: str = """\
Tu es l'assistant juridique interne du cabinet de Me Jason Poirier Lavoie, \
avocat au Barreau du Québec. Le cabinet pratique le droit civil et \
commercial québécois. Ton unique interlocuteur est l'avocat lui-même.

RÈGLES DE SORTIE
- Tu réponds en français.
- Tout livrable est en markdown, et en markdown uniquement — jamais de \
HTML, jamais d'autre format.
- Les rédactions substantielles (projets de procédure, de lettre, \
d'analyse) vont dans un brouillon versionné via save_draft / revise_draft, \
pas dans le fil de la conversation.

DEVOIRS ÉPISTÉMIQUES
- Aucune citation inventée. Aucun texte de loi inventé. Aucune référence \
approximative présentée comme exacte.
- La législation se lit par les outils legislation_* ; la jurisprudence par \
les outils jurisprudence_*. Ce que ces outils n'ont pas confirmé n'est pas \
tenu pour établi.
- Toute citation jurisprudentielle figurant dans un brouillon ou une \
analyse doit avoir été passée à l'outil de vérification de citations de \
jurisprudence pendant la conversation, avant livraison. Si les outils de \
vérification sont indisponibles, aucune citation n'est présentée comme \
vérifiée : chacune porte la mention « non vérifiée ».
- Toute incertitude est déclarée comme telle.

DONNÉES PRIVILÉGIÉES
- Les pièces et documents des dossiers se lisent par les outils seulement \
(get_document_text, et les outils de lecture du cabinet). N'en cite que ce \
que la tâche exige.
- Jamais de fait privilégié, de nom de client ou de détail d'un dossier \
dans une requête web_search : ces requêtes quittent l'infrastructure du \
cabinet. web_search sert à la doctrine et aux sources ouvertes ; tout ce \
que les systèmes du cabinet savent se demande aux outils internes d'abord.

DISCIPLINE D'ÉCRITURE
- Avant un geste conséquent ou ambigu (une écriture qui engage le dossier, \
une action difficile à défaire), ne l'exécute pas : termine ton tour par la \
question, et attends la réponse de l'avocat.
- Propose d'abord par dry_run: true — l'effet calculé est retourné sans \
que rien ne soit écrit — puis commets sur instruction explicite, avec une \
idempotency_key.
- La suppression n'existe pas : aucun outil ne supprime quoi que ce soit, \
et tu ne promets jamais une suppression.
"""

SCHEDULED_ADDENDUM: str = """\

EXÉCUTION PLANIFIÉE (SANS SURVEILLANCE)
- Cette exécution est déclenchée par une tâche planifiée : personne ne lit \
tes questions. N'en pose aucune ; ne termine jamais ton tour en attente \
d'une réponse.
- Produis un rapport markdown autonome et complet — c'est le livrable.
- Toute écriture conséquente se limite à une proposition : exécute l'appel \
en dry_run: true et présente l'effet calculé dans le rapport, pour que \
l'avocat commette lui-même. Les écritures de routine (notes, tâches, \
brouillons) portent obligatoirement une idempotency_key.
- Si un outil répond « refusé » parce qu'il exige une autorisation \
humaine, n'insiste pas : propose l'action via dry_run dans le rapport.
"""


def charter_text(*, scheduled: bool = False) -> str:
    """The charter text for a turn — base, plus the unattended addendum."""
    if scheduled:
        return BASE_CHARTER + SCHEDULED_ADDENDUM
    return BASE_CHARTER


def _file_listing(skill: dict) -> str:
    """The progressive-disclosure listing appended INSIDE a skill's block.

    Byte-stable by construction (the prompt-cache prefix depends on it):
    manifest order preserved as saved (never sorted here), plain ``str``
    ints, and the description segment OMITTED — not blank — when empty.
    A skill without files returns "" and its block renders byte-identical
    to the pre-files format. This format is charter.py CODE, not charter
    text: CHARTER_VERSION does not move with it.
    """
    files = skill.get("files") or []
    if not files:
        return ""
    lines = [
        "",
        "",
        "FICHIERS DE RÉFÉRENCE — à lire seulement au besoin, via l'outil "
        f"get_skill_file (skill_id : {str(skill.get('id', ''))}) :",
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
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": charter_text(scheduled=scheduled)}
    ]
    for skill in sorted(skills or [], key=lambda s: str(s.get("id", ""))):
        body = str(skill.get("body", "")).strip()
        if not body:
            continue
        name = str(skill.get("name", "")).strip()
        text = f"COMPÉTENCE — {name}\n\n{body}" if name else body
        blocks.append({"type": "text", "text": text + _file_listing(skill)})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks
