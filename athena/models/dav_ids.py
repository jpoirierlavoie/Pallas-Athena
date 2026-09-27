"""Client-chosen DAV identifiers: the resource name and the object UID.

A CalDAV/CardDAV client names a NEW resource in its URL
(``PUT /dav/…/<name>.ics|.vcf``) and carries its own UID in the body. The
server must store the object under BOTH, or the client's href 404s on every
later GET/PUT and a copy with another UID syncs back down — a duplicate on
the device. Four creators honour them through an explicit ``dav_id`` /
``dav_uid`` keyword (``models.task.create_task``, lot 0b B3,
``models.partie.create_partie``, lot 0b B7, ``models.hearing.create_hearing``
and ``models.note.create_note``, lot 1a), so the rule of what a client may
choose lives HERE, once: a second copy per model would drift.

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


# The characters a URI path segment may carry AS-IS (RFC 3986 « pchar »
# minus the percent-escape): unreserved, sub-delims, « : » and « @ ». Every
# href these DAV layers emit is built UNENCODED
# (``f"/dav/addressbook/{id}.vcf"``) while Flask DECODES the URL a client
# PUT to — so a stored id must never contain a character the client had to
# percent-encode, or the collection would list back a string that is not
# the client's href (not even a valid URI) and the next sync would read it
# as a new resource beside a deleted one. Deliberately ASCII.
_PATH_SEGMENT_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~!$&'()*+,;=:@"
)


def has_control_char(value: str) -> bool:
    """True when *value* holds a C0 control character or DEL."""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def valid_resource_id(resource_id: object) -> bool:
    """True when a client-chosen DAV resource name may become a document id.

    The name must be a valid URI path segment as it stands (see
    ``_PATH_SEGMENT_CHARS``): a space, « % », « ? », « # », an accent or
    any other character a client must percent-encode is refused, because
    the DAV layers echo the id back unencoded in every href. Firestore
    refuses ``.``, ``..`` and ids of the reserved ``__x__`` shape at write
    time; refusing them HERE lets the DAV layer answer a clean 400 instead
    of a store error. An empty or over-long name is refused too (« / » and
    control characters fall outside the allowed set).
    """
    if not isinstance(resource_id, str) or not resource_id:
        return False
    if len(resource_id) > RESOURCE_ID_MAX_LENGTH:
        return False
    if resource_id in (".", ".."):
        return False
    if resource_id.startswith("__") and resource_id.endswith("__"):
        return False
    return all(c in _PATH_SEGMENT_CHARS for c in resource_id)


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
