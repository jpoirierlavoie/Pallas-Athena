"""Client-chosen DAV identifiers: the resource name and the object UID.

A CalDAV/CardDAV client names a NEW resource in its URL
(``PUT /dav/…/<name>.ics|.vcf``) and carries its own UID in the body. The
server must store the object under BOTH, or the client's href 404s on every
later GET/PUT and a copy with another UID syncs back down — a duplicate on
the device. Two creators honour them through an explicit ``dav_id`` /
``dav_uid`` keyword (``models.task.create_task``, lot 0b B3, and
``models.partie.create_partie``, lot 0b B7), so the rule of what a client
may choose lives HERE, once: a second copy per model would drift.

No Firestore here — pure, importable without the client.
"""

from typing import Optional

from security import sanitize

# The DAV create path's two refusals. Constants, so the DAV layer can map
# them to their HTTP answers (400 / 412) without parsing French.
DAV_ID_INVALID = "Identifiant de ressource invalide."
DAV_ID_TAKEN = "Cet identifiant de ressource est déjà utilisé."

# A client-chosen resource name becomes a Firestore document id. DavX5 and
# jtx name new resources with a UUID (36 characters); 128 leaves room for
# the longer UID-shaped names other clients use while staying far from
# Firestore's own 1500-byte ceiling.
RESOURCE_ID_MAX_LENGTH = 128
# A UID is echoed back verbatim in every object this record is served as.
DAV_UID_MAX_LENGTH = 255


def has_control_char(value: str) -> bool:
    """True when *value* holds a C0 control character or DEL."""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def valid_resource_id(resource_id: object) -> bool:
    """True when a client-chosen DAV resource name may become a document id.

    Firestore refuses ``/``, ``.``, ``..`` and ids of the reserved
    ``__x__`` shape at write time; refusing them HERE lets the DAV layer
    answer a clean 400 instead of a store error. Control characters and an
    empty or over-long name are refused too.
    """
    if not isinstance(resource_id, str) or not resource_id:
        return False
    if len(resource_id) > RESOURCE_ID_MAX_LENGTH:
        return False
    if "/" in resource_id or resource_id in (".", ".."):
        return False
    if resource_id.startswith("__") and resource_id.endswith("__"):
        return False
    return not has_control_char(resource_id)


def client_uid(uid: object) -> Optional[str]:
    """The client's UID when it can be stored verbatim, else ``None``.

    Verbatim or not at all: a UID the store altered (tags stripped,
    truncated) would read to the client as a DIFFERENT object — the very
    duplicate the DAV create path exists to prevent — so a UID ``sanitize``
    would change is not kept, and the caller mints a fresh one.
    """
    if not isinstance(uid, str):
        return None
    uid = uid.strip()
    if not uid or len(uid) > DAV_UID_MAX_LENGTH or has_control_char(uid):
        return None
    if sanitize(uid, max_length=DAV_UID_MAX_LENGTH) != uid:
        return None
    return uid
