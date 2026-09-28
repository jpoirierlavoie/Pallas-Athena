"""Folder Firestore CRUD — logical folder hierarchy for document organization.

Folders live in the TOP-LEVEL ``folders`` collection, keyed by id, each
carrying its ``dossier_id`` — not in a ``dossiers/{id}/folders``
subcollection (CLAUDE.md described one until 2026-09-27; the code never
did). Folders are Firestore-only: a document's bytes stay at their flat
Storage path whatever folder it is filed in.

Lot 2A, step T2 (2026-09-27) — hardened before the connector reaches it:

* every rule that needs the dossier's folders (duplicate name, parent in the
  same dossier, depth, cycle) is computed in Python from ONE read of them,
  inside the transaction that writes, and that read FAILS CLOSED: a read
  error refuses the write. The duplicate check used to go through
  ``list_folders``, which answers ``[]`` on an outage — so an unreadable
  store read as « no folder of that name » and minted a duplicate, and the
  depth and cycle walks stopped silently at the first unreadable parent;
* a name is judged AS SUBMITTED and refused when storage would change it.
  It used to be validated raw, then ``sanitize()``d: « <x> » was stored as
  an empty name, « a<b>c » as « ac »;
* folders gain an ``etag`` and the provenance stamps (Architecture Rule 7's
  folder exception is lifted — the connector will edit them), and a rename
  or a move is a PARTIAL ``update()`` of its own key, optionally guarded by
  ``expected_etag`` (``models.concurrency``). Both used to ``set()`` the
  whole folder from an earlier read;
* the two SYSTEM folders — « Projets » (every generated document) and
  « Reçus du portail » (every portal versement) — are found by their
  ``system_role``, never by name, and live at a DETERMINISTIC id
  (:func:`system_folder_id`) created with ``create()``: parallel generations
  can no longer fork « Projets », and renaming or moving it (now refused)
  could no longer make the next generation mint a second one. The old
  ``get_or_create_folder`` found them by name, swallowed its creation
  errors, and duplicated on a read error; it is gone (see
  :func:`ensure_system_folder`).
"""

import hashlib
import logging
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from firebase_admin import storage
from google.api_core.exceptions import AlreadyExists
from google.cloud.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import concurrency, db, provenance
from models.document import GENERATED_FOLDER_NAME, PORTAL_FOLDER_NAME
from security import sanitize
from utils.logging_setup import log_dossier_event, log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

# Firestore collection path — TOP-LEVEL (see the module docstring).
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

# ── System folders ────────────────────────────────────────────────────────
#
# A role, not a name, identifies them. The display names stay the ones the
# application has always used (models/document.py owns them).
SYSTEM_ROLE_PROJETS = "projets"
SYSTEM_ROLE_PORTAIL = "portail"
SYSTEM_FOLDER_NAMES: dict[str, str] = {
    SYSTEM_ROLE_PROJETS: GENERATED_FOLDER_NAME,
    SYSTEM_ROLE_PORTAIL: PORTAL_FOLDER_NAME,
}
VALID_SYSTEM_ROLES: tuple[str, ...] = tuple(SYSTEM_FOLDER_NAMES)

# FROZEN FOREVER. A system folder's id is uuid5(this, "{dossier_id}:{role}");
# changing it would make every existing system folder invisible to the
# deterministic lookup and let the next generation create a second one.
_SYSTEM_FOLDER_NAMESPACE = uuid.UUID("f099e64f-83da-4661-9140-997f294dcac1")

# ── French messages ───────────────────────────────────────────────────────

READ_ERROR = (
    "Impossible de vérifier les dossiers de classement — réessayez. "
    "Rien n'a été modifié."
)
FOLDER_NOT_FOUND = "Dossier introuvable."
DOSSIER_REQUIRED = "Dossier juridique requis."
DOSSIER_NOT_FOUND = "Dossier juridique introuvable."
DUPLICATE_HERE = "Un dossier avec ce nom existe déjà à cet emplacement."
DUPLICATE_AT_DESTINATION = "Un dossier avec ce nom existe déjà à la destination."
NAME_REQUIRED = "Le nom du dossier est requis."
NAME_TOO_LONG = f"Le nom ne doit pas dépasser {MAX_NAME_LENGTH} caractères."
NAME_SLASH = "Le nom ne peut pas contenir les caractères / ou \\."
NAME_CONTROL = (
    "Le nom ne peut pas contenir de caractère invisible (saut de ligne, "
    "tabulation…)."
)
# The chevrons are named in WORDS: this message travels on ?erreur=, and the
# browser banner sanitizes it — a literal « <…> » in it would itself vanish
# (the wording models/document._text_errors settled on).
NAME_CHEVRONS = (
    "Nom du dossier : un passage entre chevrons serait retiré à "
    "l'enregistrement — retirez les chevrons."
)
SYSTEM_FOLDER_LOCKED = (
    "« {name} » est un dossier système de l'application : il ne se renomme "
    "pas et ne se déplace pas."
)
RESERVED_ROOT_NAME = (
    "« {name} » est réservé, à la racine du dossier, au dossier système de "
    "l'application. Choisissez un autre nom, ou rangez ce dossier dans un "
    "sous-dossier."
)
SUBTREE_CHANGED = (
    "Le contenu de ce dossier a changé depuis l'affichage de la page : il "
    "compte maintenant {documents} fichier{ds} et {folders} "
    "sous-dossier{fs}. Rien n'a été supprimé — vérifiez son contenu, puis "
    "confirmez de nouveau."
)


class _Refused(Exception):
    """Raised inside a transactional body: nothing is written, the French
    errors travel back to the caller (the real ``transactional`` decorator
    rolls back on any exception)."""

    def __init__(self, errors: list[str]):
        super().__init__("refused")
        self.errors = list(errors)


# ── Pure helpers ──────────────────────────────────────────────────────────


def _fold(name: object) -> str:
    """The comparison form of a folder name: trimmed, case-folded.

    Deliberately NOT Unicode-normalized: it is the lookup the deleted
    ``get_or_create_folder`` made (``strip().lower()`` against the NFC
    constants), which is what decides which LEGACY folder holds the app's
    generated documents (:func:`_legacy_candidates`). Every other comparison
    uses :func:`_fold_equivalent`.
    """
    return name.strip().casefold() if isinstance(name, str) else ""


def _fold_equivalent(name: object) -> str:
    """:func:`_fold` after NFC normalization — the form two names that LOOK
    identical share (review of T2). « Pièces » can arrive precomposed (NFC,
    U+00E8) or decomposed (NFD, e + U+0300); unnormalized, the two passed
    the duplicate check side by side, and a decomposed « Reçus du portail »
    slipped past the reserved-root-name rule.
    """
    if not isinstance(name, str):
        return ""
    return unicodedata.normalize("NFC", name).strip().casefold()


_RESERVED_ROOT_NAMES = {
    _fold_equivalent(n): role for role, n in SYSTEM_FOLDER_NAMES.items()
}


def _reserved_role(name: object) -> str:
    """The system role whose name *name* is (case-insensitively, whatever
    its Unicode normalization), or ``''``."""
    return _RESERVED_ROOT_NAMES.get(_fold_equivalent(name), "")


def _is_invisible_break(char: str) -> bool:
    """A control character (C0, DEL and C1 — Unicode category ``Cc``) or a
    line/paragraph separator (``Zl``/``Zp``, U+2028/U+2029). The review of
    T2 found U+0085 (NEL), U+2028 and U+2029 accepted: each is a line break
    to ``str.splitlines`` and to a browser, and none shows in the name."""
    return unicodedata.category(char) in ("Cc", "Zl", "Zp")


def _name_errors(name: object) -> list[str]:
    """Refuse what storage would alter — never alter it.

    *name* is the value that will be STORED (the caller trims it first).
    """
    if not isinstance(name, str) or not name.strip():
        return [NAME_REQUIRED]
    errors: list[str] = []
    if len(name) > MAX_NAME_LENGTH:
        errors.append(NAME_TOO_LONG)
    if "/" in name or "\\" in name:
        errors.append(NAME_SLASH)
    if any(_is_invisible_break(c) for c in name):
        errors.append(NAME_CONTROL)
    if len(name) <= MAX_NAME_LENGTH and sanitize(name, max_length=MAX_NAME_LENGTH) != name:
        errors.append(NAME_CHEVRONS)
    return errors


def system_folder_id(dossier_id: str, role: str) -> str:
    """The deterministic id of *dossier_id*'s system folder of *role*.

    A documented exception to Architecture Rule 6 (UUIDv4 ids, never
    reused): the id is a UUIDv5, and a system folder the lawyer deleted is
    recreated at the SAME id by the next generation. That is the point —
    two parallel callers can only ever target one document.
    """
    if role not in SYSTEM_FOLDER_NAMES:
        raise ValueError(f"unknown system folder role: {role!r}")
    return str(uuid.uuid5(_SYSTEM_FOLDER_NAMESPACE, f"{dossier_id}:{role}"))


def _age_key(folder: dict) -> tuple:
    """Oldest first: ``created_at`` ascending, a missing one last, then id."""
    created = folder.get("created_at")
    if isinstance(created, datetime):
        return (0, created, str(folder.get("id") or ""))
    return (1, 0, str(folder.get("id") or ""))


def _legacy_candidates(folders: Iterable[dict], role: str) -> list[dict]:
    """ROOT folders bearing *role*'s name and no role — the system folders
    ``get_or_create_folder`` created before system roles existed."""
    wanted = _fold(SYSTEM_FOLDER_NAMES[role])
    return [
        f for f in folders
        if not f.get("parent_folder_id")
        and not f.get("system_role")
        and _fold(f.get("name")) == wanted
    ]


def _role_holders(folders: list[dict]) -> dict[str, dict]:
    """``{role: the folder that IS that system folder}`` over one dossier.

    A folder STAMPED with the role wins (the one at the deterministic id if
    several, else the oldest). Otherwise the OLDEST legacy candidate — the
    one :func:`ensure_system_folder` would adopt. Every other folder named
    « Projets », including the duplicates a past fork left behind, is an
    ordinary folder the lawyer may rename or move.
    """
    holders: dict[str, dict] = {}
    for role in VALID_SYSTEM_ROLES:
        stamped = [f for f in folders if f.get("system_role") == role]
        if stamped:
            at_id = [
                f for f in stamped
                if f.get("id") == system_folder_id(f.get("dossier_id") or "", role)
            ]
            holders[role] = at_id[0] if at_id else min(stamped, key=_age_key)
            continue
        legacy = _legacy_candidates(folders, role)
        if legacy:
            holders[role] = min(legacy, key=_age_key)
    return holders


def is_system_folder(
    folder: Optional[dict],
    folders: Optional[Iterable[dict]] = None,
) -> bool:
    """True when *folder* is one of its dossier's system folders.

    A folder carrying a ``system_role`` always is. An unstamped folder is
    one only when it is the legacy holder of a role: a ROOT folder bearing
    the role's name, the oldest such, while no folder carries the role —
    which *folders* (the dossier's root folders suffice) lets this decide.
    Without *folders*, a root folder bearing a reserved name is PRESUMED
    system: refusing a rename by mistake costs a click, forking « Projets »
    costs the dossier's generated documents a second home.
    """
    if not folder:
        return False
    if folder.get("system_role"):
        return True
    if folder.get("parent_folder_id") or not _reserved_role(folder.get("name")):
        return False
    if folders is None:
        return True
    pool = list(folders)
    if not any(f.get("id") == folder.get("id") for f in pool):
        pool.append(folder)
    return any(
        h.get("id") == folder.get("id") for h in _role_holders(pool).values()
    )


def _index(folders: list[dict]) -> tuple[dict, dict]:
    """``(by_id, children)`` — ``children`` keyed by parent id or ``None``."""
    by_id = {f.get("id"): f for f in folders if f.get("id")}
    return by_id, _children_index(folders)


def _depth(folder_id: Optional[str], by_id: dict) -> int:
    """Levels from *folder_id* up to the root, itself included (a root
    folder is 1, ``None`` — the dossier root — is 0). Cycle-safe; a
    dangling parent id ends the walk (the read is complete, so a missing
    parent really is missing)."""
    depth = 0
    current = folder_id
    seen: set = set()
    while current and current in by_id and current not in seen:
        seen.add(current)
        depth += 1
        current = by_id[current].get("parent_folder_id")
    return depth


def _subtree_height(folder_id: str, children: dict) -> int:
    """Levels BELOW *folder_id* (0 = a leaf). Cycle-safe."""
    height = 0
    level = [folder_id]
    seen = {folder_id}
    while True:
        nxt = [
            c["id"] for fid in level for c in children.get(fid, [])
            if c.get("id") and c["id"] not in seen
        ]
        if not nxt:
            return height
        seen.update(nxt)
        height += 1
        level = nxt


def _is_within(folder_id: str, candidate: Optional[str], by_id: dict) -> bool:
    """True when *candidate* is *folder_id* or lies below it."""
    current = candidate
    seen: set = set()
    while current and current not in seen:
        if current == folder_id:
            return True
        seen.add(current)
        parent = by_id.get(current)
        if parent is None:
            return False
        current = parent.get("parent_folder_id")
    return False


def _name_taken(
    folders: list[dict],
    parent_id: Optional[str],
    name: str,
    exclude_id: Optional[str] = None,
) -> bool:
    """A folder of *name* (case-insensitive, whatever its Unicode
    normalization) already sits in *parent_id*."""
    wanted = _fold_equivalent(name)
    return any(
        (f.get("parent_folder_id") or None) == (parent_id or None)
        and f.get("id") != exclude_id
        and _fold_equivalent(f.get("name")) == wanted
        for f in folders
    )


def find_folder_named(
    folders: Iterable[dict],
    parent_id: Optional[str],
    name: object,
) -> Optional[dict]:
    """The folder of *folders* (one dossier's) sitting in *parent_id*
    (``None`` = the dossier root) under *name* — compared exactly as
    :func:`_name_taken` compares (trimmed, case-folded, whatever the Unicode
    normalization) — or ``None``. Pure.

    The connector's ``manage_folder`` create with ``if_exists: "reuse"``
    finds, through this, the folder its refused creation collided with: what
    it reuses is by construction what the duplicate rule refused."""
    wanted = _fold_equivalent(name)
    if not wanted:
        return None
    for f in folders:
        if ((f.get("parent_folder_id") or None) == (parent_id or None)
                and _fold_equivalent(f.get("name")) == wanted):
            return f
    return None


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


def _folders_query(dossier_id: str):
    return db.collection(COLLECTION).where(
        filter=FieldFilter("dossier_id", "==", dossier_id)
    )


def _all_folders(dossier_id: str) -> list[dict]:
    """Every folder of a dossier — ONE query, errors PROPAGATE.

    ``list_folders`` deliberately fails open to ``[]`` (a browser listing
    degrades to « empty »); a deletion may not, or it would report « this
    folder is empty » and destroy what it could not read — and neither may a
    duplicate-name check, or an outage reads as « no such folder yet ».
    """
    return [doc.to_dict() for doc in _folders_query(dossier_id).stream()]


def _read_folders(dossier_id: str, transaction) -> list[dict]:
    """:func:`_all_folders` inside *transaction*, a failure turned into the
    French refusal. The query runs in the transaction so that a folder
    created meanwhile aborts the commit (the real client re-runs the body on
    fresh data) instead of letting a duplicate through."""
    try:
        return [
            doc.to_dict()
            for doc in _folders_query(dossier_id).stream(transaction=transaction)
        ]
    except Exception:
        log_unexpected("folder tree read failed", dossier_id=dossier_id)
        raise _Refused([READ_ERROR])


def _read_dossier_exists(dossier_id: str, transaction=None) -> bool:
    """Whether the dossier exists — a keyed read that FAILS CLOSED."""
    try:
        snap = db.collection("dossiers").document(dossier_id).get(
            transaction=transaction
        )
    except Exception:
        log_unexpected("folder dossier check failed", dossier_id=dossier_id)
        raise _Refused([READ_ERROR])
    return bool(snap.exists)


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
        index.setdefault(f.get("parent_folder_id") or None, []).append(f)
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


def _subtree_fingerprint(folder_ids: Iterable[str], documents: Iterable[dict]) -> str:
    """sha256 over the sorted folder ids and the sorted document ids of a
    subtree — WHICH records the delete dialog announced, not just how many.

    The counts alone miss a swap: one file moved out and another moved in
    (two connector calls while the dialog is open) keep « 3 fichiers » true,
    and « Tout supprimer » would destroy the newcomer, never shown. Ids only:
    a rename inside the subtree changes nothing that the deletion acts on,
    so it must not refuse. :func:`subtree_index` and :func:`delete_folder`
    both derive it here, from the same two reads, so they cannot disagree.
    """
    folders_part = "\n".join(sorted(str(f or "") for f in folder_ids))
    docs_part = "\n".join(sorted(str(d.get("id") or "") for d in documents))
    return hashlib.sha256(
        f"{folders_part}\x00{docs_part}".encode("utf-8")
    ).hexdigest()


def subtree_index(dossier_id: str) -> dict:
    """``{folder_id: {"direct": n, "documents": n, "folders": n,
    "fingerprint": sha256}}`` for EVERY folder of the dossier, in TWO
    queries.

    ``direct`` is the one-level count the browser row already displays;
    ``documents`` / ``folders`` are the SUBTREE totals the delete dialog must
    announce — « 23 fichiers dans 4 sous-dossiers » is the whole guard rail
    the lawyer gets before an irreversible deletion, so it counts what will
    actually be destroyed, not just the top level. The dialog posts them
    back with ``fingerprint`` (:func:`_subtree_fingerprint` — which records,
    not only how many), and :func:`delete_folder` refuses when the subtree
    no longer matches them. Errors propagate.
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
        subtree_docs = [d for i in ids for d in docs_by_folder.get(i, [])]
        index[fid] = {
            "direct": len(children.get(fid, [])) + len(docs_by_folder.get(fid, [])),
            "documents": len(subtree_docs),
            "folders": len(ids) - 1,          # the subtree, target excluded
            "fingerprint": _subtree_fingerprint(ids, subtree_docs),
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
    """Create a new folder. Returns ``(folder, errors)``.

    Refused — nothing written — when the name would not be stored as given,
    when the dossier or the parent (which must belong to the SAME dossier)
    does not exist, when the depth would exceed :data:`MAX_NESTING_DEPTH`,
    when a folder of that name (case-insensitive) already sits there, when a
    ROOT folder would take a system folder's name (:func:`ensure_system_folder`
    is the only door to those), or when the folders cannot be read.

    Never creates a system folder: :func:`ensure_system_folder` does, at its
    deterministic id.
    """
    if not isinstance(dossier_id, str) or not dossier_id.strip():
        return None, [DOSSIER_REQUIRED]
    name = name.strip() if isinstance(name, str) else name
    errors = _name_errors(name)
    if errors:
        return None, errors
    parent_id = parent_folder_id or None
    if parent_id is None and _reserved_role(name):
        return None, [RESERVED_ROOT_NAME.format(name=name)]

    folder_id = str(uuid.uuid4())
    ref = db.collection(COLLECTION).document(folder_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        # Reads first (the real client refuses a read after a staged write).
        if not _read_dossier_exists(dossier_id, transaction):
            raise _Refused([DOSSIER_NOT_FOUND])
        folders = _read_folders(dossier_id, transaction)
        by_id, _children = _index(folders)
        if parent_id is not None:
            if parent_id not in by_id:
                raise _Refused(["Le dossier parent est introuvable."])
            if _depth(parent_id, by_id) >= MAX_NESTING_DEPTH:
                raise _Refused([
                    f"La profondeur maximale de {MAX_NESTING_DEPTH} niveaux "
                    "est atteinte."
                ])
        if _name_taken(folders, parent_id, name):
            raise _Refused([DUPLICATE_HERE])
        folder = {
            "id": folder_id,
            "dossier_id": dossier_id,
            "name": name,
            "parent_folder_id": parent_id,
            "order": 0,
            "system_role": "",
            **provenance.create_fields(datetime.now(timezone.utc)),
        }
        transaction.create(ref, folder)
        return folder

    try:
        folder = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors
    except Exception:
        log_unexpected("folder create failed")
        return None, ["Erreur lors de la création. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, folder_id)

    # Touch parent folder's updated_at (a timestamp, never its etag — a
    # child's creation must not turn the parent's open rename form stale).
    if parent_id:
        _touch_folder(dossier_id, parent_id)

    return folder, []


def ensure_system_folder(
    dossier_id: str,
    role: str,
) -> tuple[Optional[dict], list[str]]:
    """The dossier's system folder of *role*, created if it does not exist.

    Returns ``(folder, [])`` or ``(None, french_errors)`` — never a silent
    fallback: a caller that cannot get « Projets » REFUSES to save rather
    than dropping the document at the dossier root, which is what the three
    callers of the old ``get_or_create_folder`` did on its ``None``.

    From ONE read of the dossier's folders (failing CLOSED):

    1. a folder STAMPED with the role → it;
    2. otherwise the legacy match — the OLDEST ROOT folder bearing the
       role's name, created by name before roles existed → it is stamped
       with the role (a partial update) and returned. Several such folders
       (a past fork) are logged; the others stay ordinary folders;
    3. otherwise it is CREATED at :func:`system_folder_id`, with
       ``document(id).create()``. A parallel caller that created it first
       makes that ``create()`` raise ``AlreadyExists``, and the folder is
       read back: two generations racing on a new dossier land in ONE
       « Projets », where the read-then-``set()`` of a fresh uuid4 forked it.
    """
    if role not in SYSTEM_FOLDER_NAMES:
        raise ValueError(f"unknown system folder role: {role!r}")
    if not isinstance(dossier_id, str) or not dossier_id.strip():
        return None, [DOSSIER_REQUIRED]

    for _attempt in range(2):
        try:
            folders = _all_folders(dossier_id)
        except Exception:
            log_unexpected("system folder read failed", dossier_id=dossier_id)
            return None, [READ_ERROR]

        holder = _role_holders(folders).get(role)
        if holder is not None and holder.get("system_role") == role:
            return holder, []
        if holder is not None:
            adopted, errors, vanished = _adopt_legacy(
                dossier_id, role, holder,
                candidates=len(_legacy_candidates(folders, role)),
            )
            if vanished:
                continue          # deleted meanwhile: read again, once
            return adopted, errors
        return _create_system_folder(dossier_id, role)
    return None, [READ_ERROR]


def _adopt_legacy(
    dossier_id: str,
    role: str,
    legacy: dict,
    *,
    candidates: int,
) -> tuple[Optional[dict], list[str], bool]:
    """Stamp *legacy* with *role*. Returns ``(folder, errors, vanished)``."""
    folder_id = legacy.get("id") or ""
    ref = db.collection(COLLECTION).document(folder_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[Optional[dict], bool]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None, False
        data = snap.to_dict() or {}
        if data.get("dossier_id") != dossier_id:
            return None, False
        if data.get("system_role") == role:
            return data, False            # a racing caller stamped it
        if data.get("system_role"):
            raise _Refused([READ_ERROR])  # stamped with another role meanwhile
        fields = {
            "system_role": role,
            **provenance.update_fields(datetime.now(timezone.utc)),
        }
        transaction.update(ref, fields)
        return {**data, **fields}, True

    try:
        folder, wrote = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors, False
    except Exception:
        log_unexpected("system folder adoption failed", dossier_id=dossier_id)
        return None, ["Erreur lors de la préparation du dossier système. "
                      "Veuillez réessayer."], False
    if folder is None:
        return None, [], True
    if wrote:
        # IDEMPOTENT (lot 2A, T8): a second run finds the role stamped and
        # writes nothing — so a generation that fails AFTER this adoption
        # committed nothing a retry would repeat (models/provenance).
        provenance.note_commit(COLLECTION, folder_id, idempotent=True)
        # ONE line per dossier and role, ever: the adoption is permanent.
        # `legacy_candidates > 1` flags a past fork the lawyer may want to
        # merge by hand (the others stay ordinary folders).
        log_dossier_event(
            "system_folder_adopted", dossier_id,
            folder_id=folder_id, role=role, legacy_candidates=candidates,
        )
    return folder, [], False


def _create_system_folder(
    dossier_id: str, role: str,
) -> tuple[Optional[dict], list[str]]:
    folder_id = system_folder_id(dossier_id, role)
    ref = db.collection(COLLECTION).document(folder_id)
    try:
        exists = _read_dossier_exists(dossier_id)
    except _Refused as refusal:
        return None, refusal.errors
    if not exists:
        return None, [DOSSIER_NOT_FOUND]

    folder = {
        "id": folder_id,
        "dossier_id": dossier_id,
        "name": SYSTEM_FOLDER_NAMES[role],
        "parent_folder_id": None,
        "order": 0,
        "system_role": role,
        **provenance.create_fields(datetime.now(timezone.utc)),
    }
    try:
        ref.create(folder)
    except AlreadyExists:
        # A parallel caller created it between our read and our create():
        # the id is deterministic, so it is THE folder — read it back.
        try:
            snap = ref.get()
        except Exception:
            log_unexpected("system folder read-back failed", dossier_id=dossier_id)
            return None, [READ_ERROR]
        data = snap.to_dict() if snap.exists else None
        if not data or data.get("dossier_id") != dossier_id:
            return None, [READ_ERROR]
        return data, []
    except Exception:
        log_unexpected("system folder create failed", dossier_id=dossier_id)
        return None, ["Erreur lors de la création du dossier système. "
                      "Veuillez réessayer."]
    # IDEMPOTENT (lot 2A, T8): the id is deterministic, so a retry reads
    # this very folder back instead of creating a second — a generation that
    # fails after it (its upload) committed nothing a retry would repeat,
    # and must not be reported « ENREGISTRÉE — NE PAS RÉESSAYER ».
    provenance.note_commit(COLLECTION, folder_id, idempotent=True)
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
        logger.warning("get_folder failed for %s: %s", sanitize_log_value(folder_id), exc)
    return None


def list_folders(
    dossier_id: str,
    parent_folder_id: Optional[str] = None,
) -> list[dict]:
    """Return folders in a given parent (None = root). Sorted alphabetically.

    Fails OPEN to ``[]`` — right for a browser listing, and for nothing
    else: every write rule reads through :func:`_all_folders`.
    """
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
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], bool]:
    """Rename a folder. Returns ``(folder, errors, changed)``.

    A partial ``update()`` of ``name`` and its stamp, read and written in ONE
    transaction with the dossier's folders. Refused — nothing written — on a
    stale ``expected_etag`` (``models.concurrency``), on a system folder, on
    a name that would not be stored as given, on a duplicate in the same
    parent, on a root name reserved for a system folder, or when the folders
    cannot be read. Renaming to the same name writes nothing.
    """
    new_name = new_name.strip() if isinstance(new_name, str) else new_name
    ref = db.collection(COLLECTION).document(folder_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, bool]:
        folders = _read_folders(dossier_id, transaction)
        by_id, _children = _index(folders)
        existing = by_id.get(folder_id)
        if existing is None:
            raise _Refused([FOLDER_NOT_FOUND])
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        if is_system_folder(existing, folders):
            raise _Refused([SYSTEM_FOLDER_LOCKED.format(name=existing.get("name") or "")])
        errors = _name_errors(new_name)
        if errors:
            raise _Refused(errors)
        if existing.get("name") == new_name:
            return existing, False
        parent_id = existing.get("parent_folder_id") or None
        if parent_id is None and _reserved_role(new_name):
            raise _Refused([RESERVED_ROOT_NAME.format(name=new_name)])
        if _name_taken(folders, parent_id, new_name, exclude_id=folder_id):
            raise _Refused([DUPLICATE_HERE])
        fields = {
            "name": new_name,
            **provenance.update_fields(datetime.now(timezone.utc)),
        }
        transaction.update(ref, fields)
        return {**existing, **fields}, True

    try:
        folder, changed = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors, False
    except Exception:
        log_unexpected("folder rename failed")
        return None, ["Erreur lors du renommage. Veuillez réessayer."], False
    if changed:
        provenance.note_commit(COLLECTION, folder_id)
        if folder.get("parent_folder_id"):
            _touch_folder(dossier_id, folder["parent_folder_id"])
    return folder, [], changed


def move_folder(
    dossier_id: str,
    folder_id: str,
    new_parent_folder_id: Optional[str],
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], bool]:
    """Move a folder to a new parent. Returns ``(folder, errors, changed)``.

    A partial ``update()`` of ``parent_folder_id``, every rule computed from
    ONE read of the dossier's folders inside the writing transaction: the
    destination must be a folder of the SAME dossier, never the folder
    itself or one of its descendants, and the moved subtree must stay within
    :data:`MAX_NESTING_DEPTH`. The old walks read one parent at a time and
    stopped SILENTLY at the first unreadable one — a cycle or an over-deep
    tree then passed. System folders do not move. Moving to the current
    parent writes nothing.
    """
    target = new_parent_folder_id or None
    ref = db.collection(COLLECTION).document(folder_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, bool, Optional[str]]:
        folders = _read_folders(dossier_id, transaction)
        by_id, children = _index(folders)
        existing = by_id.get(folder_id)
        if existing is None:
            raise _Refused([FOLDER_NOT_FOUND])
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        if target == folder_id:
            raise _Refused(["Impossible de déplacer un dossier dans lui-même."])
        if is_system_folder(existing, folders):
            raise _Refused([SYSTEM_FOLDER_LOCKED.format(name=existing.get("name") or "")])
        old_parent = existing.get("parent_folder_id") or None
        if old_parent == target:
            return existing, False, old_parent
        if target is not None:
            if target not in by_id:
                raise _Refused(["Le dossier de destination est introuvable."])
            if _is_within(folder_id, target, by_id):
                raise _Refused([
                    "Impossible de déplacer un dossier dans un de ses "
                    "sous-dossiers."
                ])
            if _depth(target, by_id) + 1 + _subtree_height(folder_id, children) > MAX_NESTING_DEPTH:
                raise _Refused([
                    "Ce déplacement dépasserait la profondeur maximale de "
                    f"{MAX_NESTING_DEPTH} niveaux."
                ])
        elif _reserved_role(existing.get("name")):
            raise _Refused([RESERVED_ROOT_NAME.format(name=existing.get("name") or "")])
        if _name_taken(folders, target, existing.get("name") or "", exclude_id=folder_id):
            raise _Refused([DUPLICATE_AT_DESTINATION])
        fields = {
            "parent_folder_id": target,
            **provenance.update_fields(datetime.now(timezone.utc)),
        }
        transaction.update(ref, fields)
        return {**existing, **fields}, True, old_parent

    try:
        folder, changed, old_parent = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors, False
    except Exception:
        log_unexpected("folder move failed")
        return None, ["Erreur lors du déplacement. Veuillez réessayer."], False
    if not changed:
        return folder, [], False
    provenance.note_commit(COLLECTION, folder_id)
    # Touch old and new parent (timestamps only).
    for parent in (old_parent, target):
        if parent:
            _touch_folder(dossier_id, parent)
    return folder, [], True


# A chunk of the documents phase. Production has had no per-commit write cap
# since 2023-03-29 (tests/_fake_firestore.py cites the release note); the
# chunk only bounds how much ONE transaction rewrites — each chunk re-reads
# the subtree anyway.
_BATCH_CHUNK = 450

SUBTREE_CHANGED_DURING = (
    "Le contenu de ce dossier a changé pendant la suppression : un fichier "
    "ou un sous-dossier y a été ajouté, retiré ou déplacé entre-temps. "
    "{done}Le dossier a été conservé, avec tout ce qu'il contient encore — "
    "rechargez la page, vérifiez son contenu, puis recommencez."
)


def _documents_query(dossier_id: str):
    return db.collection("documents").where(
        filter=FieldFilter("dossier_id", "==", dossier_id)
    )


def _subtree_now(
    dossier_id: str, folder_id: str, transaction,
) -> tuple[Optional[dict], set[str], dict[str, dict]]:
    """``(the folder record, the subtree's folder ids, {doc id: document
    filed inside it})``, read THROUGH *transaction* — both queries.

    Read in the transaction that writes, so a connector write landing on
    ANY folder or document of the dossier between this read and the commit
    aborts the commit (the real client re-runs the body on fresh data, as
    the fake server does), and the re-run then sees the change and refuses.
    That is what closes the window the batched deletes of 2026-08-14 left
    open (review of T7): a ``move_documents`` or ``update_document`` refile
    INTO the subtree used to land after the read and before the folder
    records were deleted — the document then pointed at a dead
    ``folder_id``, in no folder and at no root; and in « move » mode the
    reparent write, unguarded, moved a document the connector had just
    filed ELSEWHERE back to the parent. A read failure is the French
    refusal, never « empty ».
    """
    try:
        folders = [
            s.to_dict() or {}
            for s in _folders_query(dossier_id).stream(transaction=transaction)
        ]
        documents = [
            s.to_dict() or {}
            for s in _documents_query(dossier_id).stream(transaction=transaction)
        ]
    except Exception:
        log_unexpected("folder subtree re-read failed", dossier_id=dossier_id)
        raise _Refused(["Impossible de lire le contenu du dossier. Réessayez."])
    record = next((f for f in folders if f.get("id") == folder_id), None)
    ids = set(_descendant_ids(folder_id, _children_index(folders)))
    members = {
        d["id"]: d for d in documents
        if d.get("id") and d.get("folder_id") in ids
    }
    return record, ids, members


def _refusal_message(message: str, contents: str, done: int) -> str:
    """A refusal of the documents or the folder phase, saying what the
    committed chunks ALREADY did (not undone). :data:`SUBTREE_CHANGED_DURING`
    carries the phrase in its slot; any other refusal — the subtree
    re-read failing after files were already moved or deleted — gets it
    appended (review of the fixups of lot 2A: « Impossible de lire… » alone
    hid that some files were already gone)."""
    if message == SUBTREE_CHANGED_DURING:
        return message.format(done=_done_phrase(contents, done))
    if done:
        return (f"{message} {_done_phrase(contents, done)}Le dossier a été "
                "conservé, avec tout ce qu'il contient encore.")
    return message


def _done_phrase(contents: str, count: int) -> str:
    """What the documents phase already did before a refusal — said, since
    it is not undone (each committed chunk stands)."""
    if not count:
        return "Rien n'a été modifié. "
    if count == 1:
        head = "1 fichier avait déjà été"
        tail = "supprimé" if contents == CONTENTS_DELETE else "déplacé"
    else:
        head = f"{count} fichiers avaient déjà été"
        tail = "supprimés" if contents == CONTENTS_DELETE else "déplacés"
    where = "" if contents == CONTENTS_DELETE else " vers le dossier parent"
    return f"{head} {tail}{where}. "


def delete_folder(
    dossier_id: str,
    folder_id: str,
    *,
    contents: str = CONTENTS_MOVE,
    expected_documents: Optional[int] = None,
    expected_folders: Optional[int] = None,
    expected_fingerprint: Optional[str] = None,
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

    ``expected_documents`` / ``expected_folders`` (lot 2A, T2) are the
    subtree counts the confirmation dialog ANNOUNCED, and
    ``expected_fingerprint`` (review of T2) its :func:`_subtree_fingerprint`
    — which records, not only how many (the counts alone let a SWAP
    through). When given, the deletion is refused — nothing touched — unless
    the subtree read at execution time still matches them. ``None`` asserts
    nothing (a page rendered before the dialog posted them).

    Every DESTRUCTIVE write re-reads the subtree in its own transaction
    (fixups of lot 2A — the T7 review's race, left open until then). The
    documents go by chunks, each chunk ONE transaction that re-reads the
    dossier's folders and documents (:func:`_subtree_now`) and refuses
    unless the subtree is still exactly the one announced, minus what the
    earlier chunks already handled: same folder ids, the folder still under
    the same parent, and exactly the documents still to handle — a
    document refiled INTO the subtree or OUT of it by the connector (or by
    another tab) in between refuses the rest, fail CLOSED. Then the folder
    records go in ONE transaction that re-reads the same way and refuses
    while ANY document still points into the subtree or its folder set
    changed. A write racing either transaction aborts its commit (the
    reads are in the transaction) and the re-run refuses. So no document is
    ever left under a dead ``folder_id``, a move never reverts a refile made
    meanwhile, and « Tout supprimer » never destroys a document moved out
    meanwhile — nor one moved in after the lawyer read the count.

    ORDER IS LOAD-BEARING: documents first, folder records second. A refusal
    or a failure in the document phase ABORTS without touching the folders
    (fail CLOSED): the tree stays navigable and the operation replays over
    what is left. In « delete » mode a chunk deletes the document RECORDS in
    its transaction, then their stored files — records first, the order
    ``delete_template`` uses: a file that cannot be erased is an orphan
    under the owner's prefix, referenced by nothing, with a log line; the
    old order (file first, outside any transaction) could erase the bytes
    of a document the connector had just moved out of the subtree, leaving
    a record with no file. The count of such orphans travels in the report
    (``orphaned_files``).

    Returns ``(ok, french_message, report)`` where *report* carries the
    folders and documents ACTUALLY destroyed, so the route can mint one
    deletion event per entity (the house invariant); the old
    ``(bool, str)`` return made that impossible, and only the top folder was
    ever journalled. A refusal after committed chunks still reports what
    those chunks destroyed.
    """
    if contents not in VALID_CONTENTS:
        contents = CONTENTS_MOVE

    vide: dict = {"folders": [], "documents": [], "moved": 0,
                  "orphaned_files": 0}

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

    sub_folders = len(folder_ids) - 1
    if (
        (expected_documents is not None and expected_documents != len(documents))
        or (expected_folders is not None and expected_folders != sub_folders)
        or (
            expected_fingerprint is not None
            and expected_fingerprint != _subtree_fingerprint(folder_ids, documents)
        )
    ):
        return False, SUBTREE_CHANGED.format(
            documents=len(documents), ds="s" if len(documents) != 1 else "",
            folders=sub_folders, fs="s" if sub_folders != 1 else "",
        ), vide

    if contents == CONTENTS_DELETE and len(documents) > MAX_FOLDER_DELETE_DOCUMENTS:
        return False, (
            f"Ce dossier contient {len(documents)} fichiers, au-delà de la "
            f"limite de {MAX_FOLDER_DELETE_DOCUMENTS} par suppression. "
            "Supprimez d'abord des sous-dossiers."
        ), vide

    folders_by_id = {f["id"]: f for f in _folders_by_ids(dossier_id, folder_ids)}
    announced_ids = set(folder_ids)
    remaining = {d["id"] for d in documents if d.get("id")}
    by_id = {d["id"]: d for d in documents if d.get("id")}
    order = [d["id"] for d in documents if d.get("id")]
    supprimes: list[dict] = []
    moved = 0
    orphans = 0

    def _check(transaction, expected_members: set[str]) -> None:
        record, ids, members = _subtree_now(dossier_id, folder_id, transaction)
        if (
            record is None
            or (record.get("parent_folder_id") or None) != (parent_id or None)
            or ids != announced_ids
            or set(members) != expected_members
        ):
            raise _Refused([SUBTREE_CHANGED_DURING])

    # ── 1. Documents, chunk by chunk, each chunk re-reading the subtree ──
    for start in range(0, len(order), _BATCH_CHUNK):
        chunk = order[start:start + _BATCH_CHUNK]

        @firestore.transactional
        def _apply_chunk(transaction, chunk=chunk) -> None:
            _check(transaction, set(remaining))       # reads first
            now = datetime.now(timezone.utc)
            for doc_id in chunk:
                ref = db.collection("documents").document(doc_id)
                if contents == CONTENTS_DELETE:
                    transaction.delete(ref)
                else:
                    transaction.update(ref, {
                        "folder_id": parent_id,
                        **provenance.update_fields(now),
                    })

        try:
            _apply_chunk(db.transaction())
        except _Refused as refusal:
            done = len(supprimes) if contents == CONTENTS_DELETE else moved
            message = _refusal_message(refusal.errors[0], contents, done)
            return False, message, {
                "folders": [], "documents": supprimes, "moved": moved,
                "orphaned_files": orphans,
            }
        except Exception:
            log_unexpected("folder document phase failed")
            done = len(supprimes) if contents == CONTENTS_DELETE else moved
            verb = ("supprimer" if contents == CONTENTS_DELETE else "déplacer")
            return False, (
                f"Impossible de {verb} les fichiers. "
                f"{_done_phrase(contents, done)}Le dossier a été conservé — "
                "réessayez."
            ), {"folders": [], "documents": supprimes, "moved": moved,
                "orphaned_files": orphans}

        remaining.difference_update(chunk)
        if contents == CONTENTS_DELETE:
            for doc_id in chunk:
                provenance.note_commit("documents", doc_id)
                supprimes.append(by_id[doc_id])
            orphans += _erase_files([by_id[i] for i in chunk])
        else:
            for doc_id in chunk:
                provenance.note_commit("documents", doc_id)
            moved += len(chunk)

    # ── 2. Folder records — ONE transaction, re-reading the subtree: no
    #       document may point into it any more, and no folder joined it ──
    @firestore.transactional
    def _drop_folders(transaction) -> None:
        _check(transaction, set())
        # Deepest first (the reversed pre-order): a record set cut short by
        # an error could never orphan a sub-folder under a deleted parent.
        for fid in reversed(folder_ids):
            transaction.delete(db.collection(COLLECTION).document(fid))

    try:
        _drop_folders(db.transaction())
    except _Refused as refusal:
        done = len(supprimes) if contents == CONTENTS_DELETE else moved
        message = _refusal_message(refusal.errors[0], contents, done)
        return False, message, {
            "folders": [], "documents": supprimes, "moved": moved,
            "orphaned_files": orphans,
        }
    except Exception:
        log_unexpected("folder delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer.", {
            "folders": [], "documents": supprimes, "moved": moved,
            "orphaned_files": orphans,
        }
    for fid in folder_ids:
        provenance.note_commit(COLLECTION, fid)

    if parent_id:
        _touch_folder(dossier_id, parent_id)

    return True, "", {
        "folders": [folders_by_id.get(fid, {"id": fid}) for fid in folder_ids],
        "documents": supprimes,
        "moved": moved,
        "orphaned_files": orphans,
    }


def _erase_files(documents: list[dict]) -> int:
    """Erase the stored files of documents whose RECORDS were just deleted
    — the count of those that could not be erased (orphans under the
    owner's prefix, referenced by nothing: never listed, never served).
    A file already missing is not an orphan. Never raises."""
    orphans = 0
    try:
        bucket = storage.bucket()
    except Exception:
        log_unexpected("folder delete: storage unavailable",
                       count=len(documents))
        return sum(1 for d in documents if d.get("storage_path"))
    for doc in documents:
        path = doc.get("storage_path") or ""
        if not path:
            continue
        try:
            bucket.blob(path).delete()
        except NotFound:
            continue
        except Exception:
            orphans += 1
            log_unexpected("folder delete: document file not erased",
                           document_id=doc.get("id", ""))
    return orphans


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
    """Fetch ALL folders and build a nested tree. Returns root-level nodes.

    Fails OPEN to ``[]`` — a browser listing. A caller that must tell « no
    folder » from « unreadable » reads :func:`list_dossier_folders` and
    builds with :func:`build_folder_tree`.
    """
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )
        all_folders = [doc.to_dict() for doc in query.stream()]
    except Exception:
        return []
    return build_folder_tree(all_folders)


def list_dossier_folders(dossier_id: str) -> list[dict]:
    """Every folder of *dossier_id*, flat, in store order — errors PROPAGATE.

    The public face of :func:`_all_folders` for a READ that must not pass
    an outage off as « this dossier has no folder » (the connector's
    ``list_documents`` with ``include_folders``, and the system role of a
    document's folder, which a failed read must report as UNKNOWN rather
    than as « ordinary folder »). The failure is logged here once.
    """
    try:
        return _all_folders(dossier_id)
    except Exception:
        log_unexpected("folder tree read failed", dossier_id=dossier_id)
        raise


def build_folder_tree(folders: Iterable[dict]) -> list[dict]:
    """The nested tree of *folders* (one dossier's) — pure, root-level nodes.

    Each node is a COPY of its folder with a ``children`` list; the input
    is left untouched. A folder whose parent is not among *folders* (a
    dangling reference) is a root. Members of a parent cycle — which
    :func:`move_folder` refuses to create — are reachable from no root and
    are absent from the tree, as they always were. Siblings sort by name,
    case-insensitively.
    """
    nodes = [{**f, "children": []} for f in folders if f.get("id")]
    by_id: dict[str, dict] = {n["id"]: n for n in nodes}

    roots: list[dict] = []
    for node in nodes:
        parent_id = node.get("parent_folder_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)

    def sort_tree(level: list[dict]) -> None:
        level.sort(key=lambda n: (n.get("name") or "").lower())
        for n in level:
            sort_tree(n["children"])

    sort_tree(roots)
    return roots


def system_roles(folders: Iterable[dict]) -> dict[str, str]:
    """``{folder_id: role}`` for the SYSTEM folders among *folders*.

    *folders* are ONE dossier's (all of them — the legacy rule needs the
    root folders). A folder is the system folder of a role when it carries
    that ``system_role`` (the one at the deterministic id first), or — no
    folder carrying the role — when it is the oldest ROOT folder bearing
    the role's name: the legacy « Projets » that :func:`ensure_system_folder`
    would adopt. The same rule :func:`is_system_folder` applies, so what a
    reader reports as protected is what the writers protect — including a
    SECOND folder stamped with a role (only a hand edit makes one): it is not
    the role's holder, yet ``is_system_folder`` protects every stamped
    folder, so it is reported with its stamp, never as ordinary.
    """
    pool = list(folders)
    roles = {
        holder["id"]: role
        for role, holder in _role_holders(pool).items()
        if holder.get("id")
    }
    for folder in pool:
        stamped = folder.get("system_role")
        if stamped and folder.get("id"):
            roles.setdefault(folder["id"], str(stamped))
    return roles


# ── Internal helpers ─────────────────────────────────────────────────────


def _touch_folder(dossier_id: str, folder_id: str) -> None:
    """Update a folder's updated_at timestamp — never its etag."""
    try:
        db.collection(COLLECTION).document(folder_id).update({
            "updated_at": datetime.now(timezone.utc),
        })
    except Exception as exc:
        logger.warning("_touch_folder failed for %s: %s", sanitize_log_value(folder_id), exc)
