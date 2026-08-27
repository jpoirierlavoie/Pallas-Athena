"""Fichiers de référence versionnés — la moitié partagée (Phase N).

Extrait de ``models/chat_skill.py`` le 2026-08-27, quand la charte est
devenue éditable et a hérité du même dispositif. **Dérivé, jamais
recopié** : la doctrine sha/LF, la déduplication transactionnelle et le
refus — jamais la troncature — d'un contenu trop long n'existent qu'ici,
si bien que les deux porteurs ne peuvent pas diverger.

Le stockage est adressé par contenu : ``{porteur}/{id}/fichiers/{sha256}``
tient ``{content, chars, created_at}`` write-once, et chaque VERSION porte
le manifeste ``files: [{name, description, sha256, chars}]``, recopié sur
la tête (le motif « head copy » du corps). Retirer un fichier = une
nouvelle version sans lui ; les versions antérieures gardent le leur pour
toujours, et aucun document de contenu n'est jamais supprimé.

⚠ Le CONTENU ne passe DÉLIBÉRÉMENT pas par ``security.sanitize`` : son
regex tueur de balises mutilerait du matériel de référence (un modèle de
procédure portant ``<placeholder>``, un extrait XML, du code). C'est sûr
parce que le contenu n'est jamais que rendu sous autoescape Jinja — en
``<pre>``, sans ``|safe`` ni ``|markdown`` — et renvoyé au modèle en texte
de ``tool_result`` : jamais exécuté. Seul nettoyage : les contrôles C0
sauf ``\\t`` et ``\\n``, ce qui fait disparaître ``\\r`` et normalise un
collage CRLF en LF, gardant le sha stable d'un OS à l'autre. Les
MÉTADONNÉES (nom, description), elles, passent bien par ``sanitize`` —
c'est l'asymétrie, et elle est le sujet.

Les plafonds sont contraints par le plafond de requête de
``security._enforce_request_size``. Voir la note d'arithmétique dans
``models/chat_skill.py``, et **refaire ce calcul** avant de relever l'un
d'eux — un dépassement rend une page d'erreur brute qui perd toute la
saisie.
"""

import hashlib
import logging
import re
from datetime import datetime

from security import sanitize
from utils.logging_setup import sanitize_log_value

logger = logging.getLogger(__name__)

SUBCOLLECTION = "fichiers"
FILE_NAME_MAX_LENGTH = 80
FILE_DESCRIPTION_MAX_LENGTH = 200
FILE_MAX_CHARS = 40_000
MAX_FILES = 6

# C0 controls except \t (\x09) and \n (\x0a), plus DEL — the ONLY cleaning
# file content receives (see the module docstring's sanitize deviation).
_C0_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_READ_ERROR_FR = (
    "Erreur de lecture du fichier de référence. Veuillez réessayer."
)


def clean_content(content: str) -> str:
    """VERBATIM except C0 controls (\\t and \\n kept) — never sanitize()."""
    return _C0_RE.sub("", content)


def format_int_fr(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def validate_files(files: object) -> tuple[list[dict], list[str]]:
    """Normalize the submitted file rows → (entries, French errors).

    Entries carry ``{name, description, content, sha256, chars}``; the
    manifest strips ``content`` before anything persists on head/version.
    Over-long content is REFUSED, never truncated (a silently shortened
    reference file is worse than an error the user can fix in place).
    """
    if files is None:
        return [], []
    if not isinstance(files, list):
        return [], ["Le format des fichiers de référence est invalide."]
    errors: list[str] = []
    entries: list[dict] = []
    if len(files) > MAX_FILES:
        errors.append(f"Au plus {MAX_FILES} fichiers de référence.")
    seen: set[str] = set()
    for position, raw in enumerate(files, start=1):
        if not isinstance(raw, dict):
            errors.append("Le format des fichiers de référence est invalide.")
            continue
        name = sanitize(
            str(raw.get("name", "")), max_length=FILE_NAME_MAX_LENGTH
        ).strip()
        description = sanitize(
            str(raw.get("description", "")),
            max_length=FILE_DESCRIPTION_MAX_LENGTH,
        ).strip()
        content = clean_content(str(raw.get("content", "")))
        if not name:
            errors.append(
                f"Le fichier de référence n° {position} doit porter un nom."
            )
            continue
        if name.casefold() in seen:
            errors.append(
                "Deux fichiers de référence portent le même nom : "
                f"« {name} »."
            )
            continue
        seen.add(name.casefold())
        if not content.strip():
            errors.append(f"Le fichier « {name} » est vide.")
            continue
        if len(content) > FILE_MAX_CHARS:
            errors.append(
                f"Le fichier « {name} » dépasse "
                f"{format_int_fr(FILE_MAX_CHARS)} caractères "
                f"({format_int_fr(len(content))})."
            )
            continue
        entries.append(
            {
                "name": name,
                "description": description,
                "content": content,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "chars": len(content),
            }
        )
    return entries, errors


def manifest(entries: list[dict]) -> list[dict]:
    """The persisted shape — NEVER carries content (the 1 MiB/doc guard)."""
    return [
        {
            "name": e["name"],
            "description": e["description"],
            "sha256": e["sha256"],
            "chars": e["chars"],
        }
        for e in entries
    ]


def content_writes(entries: list[dict], now: datetime) -> dict[str, dict]:
    """sha-deduped {sha: payload} — Firestore refuses writing the SAME doc
    twice in one transaction, and two files with identical content share a
    sha (legal: they collapse to one content doc; names stay distinct in
    the manifest)."""
    writes: dict[str, dict] = {}
    for entry in entries:
        writes[entry["sha256"]] = {
            "content": entry["content"],
            "chars": entry["chars"],
            "created_at": now,
        }
    return writes


def file_ref(client, collection: str, doc_id: str, sha256: str):
    """The content doc's reference.

    The Firestore CLIENT is a parameter, never a module-level import: the
    carrier module owns it, so a test harness that swaps ``chat_skill.db``
    (or ``chat_charter.db``) for a fake reaches this seam too. Holding our
    own ``db`` here would have created a second module every harness must
    remember to patch — the exact footgun this file exists to remove.
    """
    return (
        client.collection(collection)
        .document(doc_id)
        .collection(SUBCOLLECTION)
        .document(sha256)
    )


def list_contents(
    client, collection: str, doc_id: str, rows: list[dict]
) -> list[dict]:
    """The UI seam (form seeding + detail view): manifest entries with
    their content attached. Fails OPEN per file — a missing or unreadable
    content doc yields ``content: ""`` + ``missing: True`` rather than
    breaking the page (the manifest still tells the truth about what the
    version references)."""
    out: list[dict] = []
    for entry in rows or []:
        row = {
            "name": entry.get("name", ""),
            "description": entry.get("description", ""),
            "sha256": entry.get("sha256", ""),
            "chars": int(entry.get("chars") or 0),
            "content": "",
            "missing": True,
        }
        try:
            snap = file_ref(client, collection, doc_id, row["sha256"]).get()
            if snap.exists:
                row["content"] = (snap.to_dict() or {}).get("content", "")
                row["missing"] = False
        except Exception as exc:
            logger.warning(
                "reference file read failed for %s/%s: %s",
                collection,
                sanitize_log_value(doc_id),
                exc,
            )
        out.append(row)
    return out


def read_from_manifest(
    client, collection: str, doc_id: str, rows: list[dict], filename: str
) -> tuple[str | None, str | None]:
    """The executor seam's second half: a file's content, by NAME, out of
    an already-resolved manifest.

    Returns ``(content, None)`` on success, ``(None, reason_fr)``
    otherwise — the unknown-name reason LISTS the available names
    (non-privileged metadata already in the prompt) so the model can
    self-correct. The caller owns resolving WHICH manifest: a pinned
    version, never a re-read head.
    """
    wanted = str(filename or "").strip()
    if not rows:
        return None, "Aucun fichier de référence n'est attaché."
    match = next(
        (
            e
            for e in rows
            if str(e.get("name", "")).casefold() == wanted.casefold()
        ),
        None,
    )
    if match is None:
        names = ", ".join(str(e.get("name", "")) for e in rows)
        return None, f"Fichier inconnu. Fichiers disponibles : {names}."
    try:
        snap = file_ref(
            client, collection, doc_id, str(match.get("sha256", ""))
        ).get()
    except Exception as exc:
        logger.warning(
            "reference file content read failed for %s/%s: %s",
            collection,
            sanitize_log_value(doc_id),
            exc,
        )
        return None, _READ_ERROR_FR
    if not snap.exists:
        # The manifest references a content doc that is absent — storage
        # incoherence worth a loud line (content docs are write-once and
        # nothing removes them).
        from utils.logging_setup import log_unexpected

        log_unexpected("chat reference file content missing", exc_info=False)
        return None, "Fichier illisible (incohérence de stockage)."
    return (snap.to_dict() or {}).get("content", ""), None
