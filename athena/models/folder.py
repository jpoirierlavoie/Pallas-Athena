"""Folder Firestore CRUD — logical folder hierarchy for document organization."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "folders"

# Constraints
MAX_NAME_LENGTH = 100
MAX_NESTING_DEPTH = 5

# Ceiling on « delete the contents too ». Each document costs THREE serial
# round trips — get_document, blob.delete(), the Firestore delete — and
# gunicorn kills the request at 60 s (app.yaml). MAX_ZIP_FILES sets the
# house rate at 150 initiations ≈ 15 s, i.e. ~100 ms each, so 200 documents
# (600 round trips) lands ON the kill point; 150 stays around 45 s. The
# margin matters more here than anywhere else: a handled failure reports
# what it already destroyed and the route journals it, but a SIGKILL
# reports NOTHING — files gone from GCS and Firestore with no audit_events
# row and no message. Refuse loudly, name the count, keep the same ceiling
# as the ZIP export.
MAX_FOLDER_DELETE_DOCUMENTS = 150

# What to do with the documents a deleted folder contains.
CONTENTS_MOVE = "move"
CONTENTS_DELETE = "delete"
VALID_CONTENTS = (CONTENTS_MOVE, CONTENTS_DELETE)


def _validate_name(name: str) -> list[str]:
    """Validate folder name. Returns list of error messages."""
    errors: list[str] = []
    if not name or not name.strip():
        errors.append("Le nom du dossier est requis.")
        return errors
    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"Le nom ne doit pas dépasser {MAX_NAME_LENGTH} caractères.")
    if "/" in name or "\\" in name:
        errors.append("Le nom ne peut pas contenir les caractères / ou \\.")
    return errors


def _check_duplicate_name(
    dossier_id: str,
    name: str,
    parent_folder_id: Optional[str],
    exclude_folder_id: Optional[str] = None,
) -> bool:
    """Return True if a folder with the same name exists in the same parent."""
    siblings = list_folders(dossier_id, parent_folder_id=parent_folder_id)
    name_lower = name.strip().lower()
    for f in siblings:
        if f.get("name", "").strip().lower() == name_lower:
            if exclude_folder_id and f.get("id") == exclude_folder_id:
                continue
            return True
    return False


def _get_depth(dossier_id: str, folder_id: Optional[str]) -> int:
    """Return the depth of a folder (0 = root level). Guards against loops."""
    depth = 0
    current = folder_id
    visited: set[str] = set()
    while current:
        if current in visited or depth > MAX_NESTING_DEPTH + 2:
            break
        visited.add(current)
        folder = get_folder(dossier_id, current)
        if not folder:
            break
        depth += 1
        current = folder.get("parent_folder_id")
    return depth


# _count_items (one query per folder, and fail-OPEN: doc_count = 0 on a read
# error, so a populated folder read as empty) was removed in August 2026. Its
# last caller, the browser's per-folder counts, moved to subtree_index, and
# the emptiness guard that justified it no longer exists — delete_folder now
# takes the whole subtree explicitly. Do not reinstate it: a fail-open
# counter is exactly what must never feed a destructive dialog, and leaving
# one in this module invites the reuse the comments below exist to prevent.


# ── Subtree enumeration (TWO queries for a whole dossier) ─────────────────
#
# The idiom build_folder_zip_url already proved (models/document.py): read
# every folder once and every document once, then work in Python — never one
# query per node. Both readers below fail CLOSED (they propagate), because
# they feed a destructive confirmation and a destructive write.


def _all_folders(dossier_id: str) -> list[dict]:
    """Every folder of a dossier — ONE query, errors PROPAGATE.

    ``list_folders`` deliberately fails open to ``[]`` (a browser listing
    degrades to « empty »); a deletion may not, or it would report « this
    folder is empty » and destroy what it could not read.
    """
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("dossier_id", "==", dossier_id)
    )
    return [doc.to_dict() for doc in query.stream()]


def _all_documents(dossier_id: str) -> list[dict]:
    """Every document of a dossier — ONE query, errors PROPAGATE.

    Deliberately NOT ``models.document.list_documents``: that one ends in
    ``except Exception: return []``, which is right for a browser listing
    and catastrophic here. An unreadable documents collection would read as
    « this folder holds nothing », the dialog would say « Ce dossier est
    vide », and the folder records would be deleted over documents still
    pointing at them — the dead-``folder_id`` bug this whole change exists
    to remove, reintroduced through the back door.
    """
    query = db.collection("documents").where(
        filter=FieldFilter("dossier_id", "==", dossier_id)
    )
    return [doc.to_dict() for doc in query.stream()]


def _children_index(folders: list[dict]) -> dict:
    """{parent_folder_id or None: [folder, …]} — a plain adjacency map."""
    index: dict = {}
    for f in folders:
        index.setdefault(f.get("parent_folder_id"), []).append(f)
    return index


def _descendant_ids(folder_id: str, children: dict) -> list[str]:
    """Ids of *folder_id* and every folder below it (cycle-safe)."""
    out: list[str] = []
    seen: set[str] = set()
    stack = [folder_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        out.append(current)
        stack.extend(c["id"] for c in children.get(current, []))
    return out


def subtree_index(dossier_id: str) -> dict:
    """``{folder_id: {"direct": n, "documents": n, "folders": n}}`` for EVERY
    folder of the dossier, in TWO queries.

    ``direct`` is the one-level count the browser row already displays;
    ``documents`` / ``folders`` are the SUBTREE totals the delete dialog must
    announce — « 23 fichiers dans 4 sous-dossiers » is the whole guard rail
    the lawyer gets before an irreversible deletion, so it counts what will
    actually be destroyed, not just the top level. Errors propagate.
    """
    folders = _all_folders(dossier_id)
    documents = _all_documents(dossier_id)

    children = _children_index(folders)
    docs_by_folder: dict = {}
    for d in documents:
        docs_by_folder.setdefault(d.get("folder_id"), []).append(d)

    index: dict = {}
    for f in folders:
        fid = f["id"]
        ids = _descendant_ids(fid, children)
        doc_total = sum(len(docs_by_folder.get(i, [])) for i in ids)
        index[fid] = {
            "direct": len(children.get(fid, [])) + len(docs_by_folder.get(fid, [])),
            "documents": doc_total,
            "folders": len(ids) - 1,          # the subtree, target excluded
        }
    return index


def subtree_members(dossier_id: str, folder_id: str) -> tuple[list[str], list[dict]]:
    """``(folder ids of the subtree — target INCLUDED, documents inside it)``.

    A document whose ``folder_id`` points INTO the subtree is included even
    if intermediate state is odd: matching on the id set is what still
    reaches a document stranded by an earlier half-failed deletion, which no
    browser view can show (``list_documents`` filters on exact equality).
    Errors propagate — see :func:`_all_documents` for why the fail-open
    reader is deliberately not used here.
    """
    folders = _all_folders(dossier_id)
    children = _children_index(folders)
    ids = _descendant_ids(folder_id, children)
    id_set = set(ids)
    documents = [
        d for d in _all_documents(dossier_id)
        if d.get("folder_id") in id_set
    ]
    return ids, documents


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_folder(
    dossier_id: str,
    name: str,
    parent_folder_id: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Create a new folder. Returns (folder, errors)."""
    name = name.strip()
    errors = _validate_name(name)
    if errors:
        return None, errors

    # Validate parent exists
    if parent_folder_id:
        parent = get_folder(dossier_id, parent_folder_id)
        if not parent:
            return None, ["Le dossier parent est introuvable."]
        # Check nesting depth
        parent_depth = _get_depth(dossier_id, parent_folder_id)
        if parent_depth >= MAX_NESTING_DEPTH:
            return None, [f"La profondeur maximale de {MAX_NESTING_DEPTH} niveaux est atteinte."]

    # Check duplicate name
    if _check_duplicate_name(dossier_id, name, parent_folder_id):
        return None, ["Un dossier avec ce nom existe déjà à cet emplacement."]

    now = datetime.now(timezone.utc)
    folder_id = str(uuid.uuid4())
    folder = {
        "id": folder_id,
        "dossier_id": dossier_id,
        "name": sanitize(name, max_length=MAX_NAME_LENGTH),
        "parent_folder_id": parent_folder_id,
        "order": 0,
        "created_at": now,
        "updated_at": now,
    }

    try:
        db.collection(COLLECTION).document(folder_id).set(folder)
    except Exception:
        log_unexpected("folder create failed")
        return None, ["Erreur lors de la création. Veuillez réessayer."]

    # Touch parent folder's updated_at
    if parent_folder_id:
        _touch_folder(dossier_id, parent_folder_id)

    return folder, []


def get_or_create_folder(
    dossier_id: str,
    name: str,
    parent_folder_id: Optional[str] = None,
) -> Optional[dict]:
    """Return the folder of *name* in *parent* (case-insensitive), or create it.

    Idempotent convenience wrapper over :func:`create_folder` (Phase H.2 §8):
    repeated note-d'honoraires generations reuse one « Notes d'honoraires »
    folder instead of tripping the duplicate-name check. Returns the folder
    dict, or ``None`` if creation failed.
    """
    name_lower = name.strip().lower()
    for existing in list_folders(dossier_id, parent_folder_id=parent_folder_id):
        if (existing.get("name") or "").strip().lower() == name_lower:
            return existing
    folder, _errors = create_folder(dossier_id, name, parent_folder_id)
    return folder


def get_folder(dossier_id: str, folder_id: str) -> Optional[dict]:
    """Fetch a single folder by ID, verifying it belongs to the dossier."""
    try:
        doc = db.collection(COLLECTION).document(folder_id).get()
        if doc.exists:
            data = doc.to_dict()
            if data.get("dossier_id") == dossier_id:
                return data
    except Exception as exc:
        logger.warning("get_folder failed for %s: %s", sanitize_log_value(folder_id), exc)
    return None


def list_folders(
    dossier_id: str,
    parent_folder_id: Optional[str] = None,
) -> list[dict]:
    """Return folders in a given parent (None = root). Sorted alphabetically."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )

        results = [doc.to_dict() for doc in query.stream()]

        # Filter by parent in Python (Firestore can't query None equality well)
        results = [
            f for f in results
            if f.get("parent_folder_id") == parent_folder_id
        ]

        results.sort(key=lambda f: (f.get("name") or "").lower())
        return results
    except Exception:
        return []


def rename_folder(
    dossier_id: str,
    folder_id: str,
    new_name: str,
) -> tuple[Optional[dict], list[str]]:
    """Rename a folder. Returns (updated_folder, errors)."""
    new_name = new_name.strip()
    errors = _validate_name(new_name)
    if errors:
        return None, errors

    existing = get_folder(dossier_id, folder_id)
    if not existing:
        return None, ["Dossier introuvable."]

    # Check duplicate in same parent
    if _check_duplicate_name(
        dossier_id, new_name, existing.get("parent_folder_id"),
        exclude_folder_id=folder_id,
    ):
        return None, ["Un dossier avec ce nom existe déjà à cet emplacement."]

    now = datetime.now(timezone.utc)
    existing["name"] = sanitize(new_name, max_length=MAX_NAME_LENGTH)
    existing["updated_at"] = now

    try:
        db.collection(COLLECTION).document(folder_id).set(existing)
    except Exception:
        log_unexpected("folder rename failed")
        return None, ["Erreur lors du renommage. Veuillez réessayer."]

    # Touch parent
    if existing.get("parent_folder_id"):
        _touch_folder(dossier_id, existing["parent_folder_id"])

    return existing, []


def move_folder(
    dossier_id: str,
    folder_id: str,
    new_parent_folder_id: Optional[str],
) -> tuple[Optional[dict], list[str]]:
    """Move a folder to a new parent. Returns (updated_folder, errors)."""
    existing = get_folder(dossier_id, folder_id)
    if not existing:
        return None, ["Dossier introuvable."]

    # Can't move to itself
    if new_parent_folder_id == folder_id:
        return None, ["Impossible de déplacer un dossier dans lui-même."]

    # Same location — no-op
    if existing.get("parent_folder_id") == new_parent_folder_id:
        return existing, []

    # Validate new parent exists
    if new_parent_folder_id:
        new_parent = get_folder(dossier_id, new_parent_folder_id)
        if not new_parent:
            return None, ["Le dossier de destination est introuvable."]

        # Prevent circular reference: walk up from new_parent to root
        current = new_parent_folder_id
        visited: set[str] = set()
        while current:
            if current == folder_id:
                return None, ["Impossible de déplacer un dossier dans un de ses sous-dossiers."]
            if current in visited:
                break
            visited.add(current)
            parent = get_folder(dossier_id, current)
            if not parent:
                break
            current = parent.get("parent_folder_id")

        # Check depth
        # Depth of new parent + 1 (this folder) + max subtree depth of this folder
        subtree_depth = _get_max_subtree_depth(dossier_id, folder_id)
        new_parent_depth = _get_depth(dossier_id, new_parent_folder_id)
        if new_parent_depth + 1 + subtree_depth > MAX_NESTING_DEPTH:
            return None, [f"Ce déplacement dépasserait la profondeur maximale de {MAX_NESTING_DEPTH} niveaux."]

    # Check duplicate name in new parent
    if _check_duplicate_name(
        dossier_id, existing["name"], new_parent_folder_id,
        exclude_folder_id=folder_id,
    ):
        return None, ["Un dossier avec ce nom existe déjà à la destination."]

    now = datetime.now(timezone.utc)
    old_parent = existing.get("parent_folder_id")
    existing["parent_folder_id"] = new_parent_folder_id
    existing["updated_at"] = now

    try:
        db.collection(COLLECTION).document(folder_id).set(existing)
    except Exception:
        log_unexpected("folder move failed")
        return None, ["Erreur lors du déplacement. Veuillez réessayer."]

    # Touch old and new parent
    if old_parent:
        _touch_folder(dossier_id, old_parent)
    if new_parent_folder_id:
        _touch_folder(dossier_id, new_parent_folder_id)

    return existing, []


_BATCH_CHUNK = 450          # Firestore caps a batch at 500 operations


def delete_folder(
    dossier_id: str,
    folder_id: str,
    *,
    contents: str = CONTENTS_MOVE,
) -> tuple[bool, str, dict]:
    """Delete a folder and its whole subtree. ``contents`` decides the files.

    * ``"move"`` (the default, and the fallback for ANY unrecognised value —
      a forged or missing form field must never destroy): every document of
      the subtree moves to the deleted folder's parent, in ONE write each.
      The previous implementation bubbled documents up one level per
      recursion step, so a document three levels down was rewritten three
      times, minting three etags.
    * ``"delete"``: every document of the subtree is deleted, GCS bytes
      included. This is the application's ONLY destructive cascade, and it
      exists solely because the user is asked first and told the count.

    ORDER IS LOAD-BEARING: documents first, folder records second. A failure
    in the document phase ABORTS without touching the folders (fail CLOSED).
    The old code swallowed the reparent failure and deleted the folder
    anyway, leaving documents with a dead ``folder_id`` — invisible in the
    browser (``list_documents`` filters on exact equality), reachable only
    through a free-text search or the whole-dossier ZIP.

    Returns ``(ok, french_message, report)`` where *report* carries the
    folders and documents ACTUALLY destroyed, so the route can mint one
    deletion event per entity (the house invariant); the old
    ``(bool, str)`` return made that impossible, and only the top folder was
    ever journalled.
    """
    if contents not in VALID_CONTENTS:
        contents = CONTENTS_MOVE

    vide: dict = {"folders": [], "documents": [], "moved": 0}

    existing = get_folder(dossier_id, folder_id)
    if not existing:
        return False, "Dossier introuvable.", vide
    parent_id = existing.get("parent_folder_id")

    # Fail CLOSED: an unreadable subtree is never « an empty subtree ».
    try:
        folder_ids, documents = subtree_members(dossier_id, folder_id)
    except Exception:
        log_unexpected("folder subtree read failed")
        return False, "Impossible de lire le contenu du dossier. Réessayez.", vide

    if contents == CONTENTS_DELETE and len(documents) > MAX_FOLDER_DELETE_DOCUMENTS:
        return False, (
            f"Ce dossier contient {len(documents)} fichiers, au-delà de la "
            f"limite de {MAX_FOLDER_DELETE_DOCUMENTS} par suppression. "
            "Supprimez d'abord des sous-dossiers."
        ), vide

    folders_by_id = {f["id"]: f for f in _folders_by_ids(dossier_id, folder_ids)}
    supprimes: list[dict] = []
    moved = 0

    # ── 1. Documents ──────────────────────────────────────────────────
    if contents == CONTENTS_DELETE:
        from models.document import delete_document

        for doc in documents:
            ok, erreur = delete_document(doc.get("id", ""))
            if not ok:
                # Stop here and keep every folder record: the tree stays
                # navigable and the operation replays over what is left.
                return False, (
                    f"{len(supprimes)} fichier(s) supprimé(s), puis : {erreur} "
                    "Le dossier a été conservé — réessayez."
                ), {"folders": [], "documents": supprimes, "moved": 0}
            supprimes.append(doc)
    elif documents:
        try:
            now = datetime.now(timezone.utc)
            for start in range(0, len(documents), _BATCH_CHUNK):
                batch = db.batch()
                for doc in documents[start:start + _BATCH_CHUNK]:
                    ref = db.collection("documents").document(doc["id"])
                    batch.update(ref, {
                        "folder_id": parent_id,
                        "updated_at": now,
                        "etag": str(uuid.uuid4()),
                    })
                batch.commit()
            moved = len(documents)
        except Exception:
            log_unexpected("folder document reparent failed")
            return False, (
                "Impossible de déplacer les fichiers. Le dossier a été "
                "conservé — réessayez."
            ), vide

    # ── 2. Folder records (the subtree, deepest first is irrelevant —
    #       the whole set goes in one commit) ────────────────────────────
    try:
        for start in range(0, len(folder_ids), _BATCH_CHUNK):
            batch = db.batch()
            for fid in folder_ids[start:start + _BATCH_CHUNK]:
                batch.delete(db.collection(COLLECTION).document(fid))
            batch.commit()
    except Exception:
        log_unexpected("folder delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer.", {
            "folders": [], "documents": supprimes, "moved": moved,
        }

    if parent_id:
        _touch_folder(dossier_id, parent_id)

    return True, "", {
        "folders": [folders_by_id.get(fid, {"id": fid}) for fid in folder_ids],
        "documents": supprimes,
        "moved": moved,
    }


def _folders_by_ids(dossier_id: str, folder_ids: list[str]) -> list[dict]:
    """The folder docs behind *folder_ids* — for the deletion trail's titles.

    Best-effort: a name that cannot be read costs a nameless trail entry,
    never the deletion itself.
    """
    try:
        wanted = set(folder_ids)
        return [f for f in _all_folders(dossier_id) if f.get("id") in wanted]
    except Exception:
        logger.warning("folder titles unreadable for the deletion trail")
        return []


# ── Navigation helpers ────────────────────────────────────────────────────


def get_folder_breadcrumb(
    dossier_id: str,
    folder_id: Optional[str],
) -> list[dict]:
    """Walk up from folder_id to root. Returns [{id, name}, ...] root-first."""
    if not folder_id:
        return []

    crumbs: list[dict] = []
    current = folder_id
    visited: set[str] = set()

    while current and len(crumbs) < MAX_NESTING_DEPTH + 2:
        if current in visited:
            break
        visited.add(current)
        folder = get_folder(dossier_id, current)
        if not folder:
            break
        crumbs.append({"id": folder["id"], "name": folder["name"]})
        current = folder.get("parent_folder_id")

    crumbs.reverse()
    return crumbs


def get_folder_tree(dossier_id: str) -> list[dict]:
    """Fetch ALL folders and build a nested tree. Returns root-level nodes."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )
        all_folders = [doc.to_dict() for doc in query.stream()]
    except Exception:
        return []

    # Build lookup
    by_id: dict[str, dict] = {}
    for f in all_folders:
        f["children"] = []
        by_id[f["id"]] = f

    roots: list[dict] = []
    for f in all_folders:
        parent_id = f.get("parent_folder_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(f)
        else:
            roots.append(f)

    # Sort children recursively
    def sort_tree(nodes: list[dict]) -> None:
        nodes.sort(key=lambda n: (n.get("name") or "").lower())
        for n in nodes:
            sort_tree(n["children"])

    sort_tree(roots)
    return roots


# ── Internal helpers ─────────────────────────────────────────────────────


def _touch_folder(dossier_id: str, folder_id: str) -> None:
    """Update a folder's updated_at timestamp."""
    try:
        db.collection(COLLECTION).document(folder_id).update({
            "updated_at": datetime.now(timezone.utc),
        })
    except Exception as exc:
        logger.warning("_touch_folder failed for %s: %s", sanitize_log_value(folder_id), exc)


def _get_max_subtree_depth(dossier_id: str, folder_id: str) -> int:
    """Return the maximum depth of the subtree rooted at folder_id (0 = leaf)."""
    children = list_folders(dossier_id, parent_folder_id=folder_id)
    if not children:
        return 0
    return 1 + max(
        _get_max_subtree_depth(dossier_id, c["id"]) for c in children
    )
