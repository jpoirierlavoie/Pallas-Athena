"""Folder Firestore CRUD — logical folder hierarchy for document organization."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "folders"

# Constraints
MAX_NAME_LENGTH = 100
MAX_NESTING_DEPTH = 5


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


def _count_items(dossier_id: str, folder_id: str) -> dict:
    """Count child folders and documents in a folder."""
    child_folders = list_folders(dossier_id, parent_folder_id=folder_id)
    try:
        query = db.collection("documents").where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        ).where(
            filter=FieldFilter("folder_id", "==", folder_id)
        )
        doc_count = sum(1 for _ in query.stream())
    except Exception:
        doc_count = 0
    return {"folders": len(child_folders), "documents": doc_count}


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
    except Exception as exc:
        return None, [f"Erreur lors de la création : {exc}"]

    # Touch parent folder's updated_at
    if parent_folder_id:
        _touch_folder(dossier_id, parent_folder_id)

    return folder, []


def get_folder(dossier_id: str, folder_id: str) -> Optional[dict]:
    """Fetch a single folder by ID, verifying it belongs to the dossier."""
    try:
        doc = db.collection(COLLECTION).document(folder_id).get()
        if doc.exists:
            data = doc.to_dict()
            if data.get("dossier_id") == dossier_id:
                return data
    except Exception as exc:
        logger.warning("get_folder failed for %s: %s", folder_id, exc)
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
    except Exception as exc:
        return None, [f"Erreur lors du renommage : {exc}"]

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
    except Exception as exc:
        return None, [f"Erreur lors du déplacement : {exc}"]

    # Touch old and new parent
    if old_parent:
        _touch_folder(dossier_id, old_parent)
    if new_parent_folder_id:
        _touch_folder(dossier_id, new_parent_folder_id)

    return existing, []


def delete_folder(
    dossier_id: str,
    folder_id: str,
    recursive: bool = False,
) -> tuple[bool, str]:
    """Delete a folder. Returns (success, error_message)."""
    existing = get_folder(dossier_id, folder_id)
    if not existing:
        return False, "Dossier introuvable."

    items = _count_items(dossier_id, folder_id)
    child_folders = list_folders(dossier_id, parent_folder_id=folder_id)
    has_content = items["folders"] > 0 or items["documents"] > 0

    if has_content and not recursive:
        return False, "Le dossier n'est pas vide."

    parent_id = existing.get("parent_folder_id")

    if recursive:
        # Recursively handle child folders first
        for child in child_folders:
            delete_folder(dossier_id, child["id"], recursive=True)

        # Move all documents in this folder to the parent (or root)
        try:
            query = db.collection("documents").where(
                filter=FieldFilter("dossier_id", "==", dossier_id)
            ).where(
                filter=FieldFilter("folder_id", "==", folder_id)
            )
            now = datetime.now(timezone.utc)
            for doc_snap in query.stream():
                db.collection("documents").document(doc_snap.id).update({
                    "folder_id": parent_id,
                    "updated_at": now,
                    "etag": str(uuid.uuid4()),
                })
        except Exception as exc:
            logger.warning("delete_folder: failed to reparent documents under %s: %s", folder_id, exc)

    # Delete the folder
    try:
        db.collection(COLLECTION).document(folder_id).delete()
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"

    # Touch parent
    if parent_id:
        _touch_folder(dossier_id, parent_id)

    return True, ""


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
        logger.warning("_touch_folder failed for %s: %s", folder_id, exc)


def _get_max_subtree_depth(dossier_id: str, folder_id: str) -> int:
    """Return the maximum depth of the subtree rooted at folder_id (0 = leaf)."""
    children = list_folders(dossier_id, parent_folder_id=folder_id)
    if not children:
        return 0
    return 1 + max(
        _get_max_subtree_depth(dossier_id, c["id"]) for c in children
    )
