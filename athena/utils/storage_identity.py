"""Storage identity: the uid every Storage path is written under.

Every object the application stores lives under a uid-keyed prefix —
``users/{uid}/dossiers/…`` (documents), ``users/{uid}/templates/…``
(gabarits), ``users/{uid}/administration/…`` (receipts), and
``staging/{uid}/…`` (the direct-to-GCS upload sessions and the zip
exports). Until 2026-09-25 each route read that uid as
``session.get("user_id", "unknown")``: under ``@login_required`` the
fallback never fired, but it was a loaded default — a caller WITHOUT a
browser session (the connector, a script, a future scheduled job) would
have written the lawyer's files under ``users/unknown/`` in perfect
silence, where the stored ``storage_path`` still resolves and nothing
would ever look wrong. ``doc_template.update_template`` carried the same
fallback in a model. Plan rule 8 removes the idiom rather than guarding
it: nothing here can ever answer ``"unknown"``.

Three functions, one per question:

* :func:`owner_uid` — the Firebase uid of the ONE authorized user
  (Architecture Rule 1), looked up with
  ``firebase_admin.auth.get_user_by_email(Config.AUTHORIZED_USER_EMAIL)``
  and memoized per process, KEYED on the email so a configuration change
  (or a test) never reads a stale answer. For callers that have no session
  — the connector's future file writes (Lots 2 and 3). The main service's
  runtime account already holds the permission: ``services/
  portail_emission.py`` calls the same lookup in production.
* :func:`request_uid` — the uid a request writes under: the SESSION's
  (verified by ``auth.py`` from the ID token at sign-in) when there is one,
  :func:`owner_uid` otherwise. What the routes call.
* :func:`require_uid` — the guard every path BUILDER applies to the uid it
  was handed, whoever computed it. It refuses the empty string, ``None``, a
  non-string, a placeholder a fallback idiom would produce (``"unknown"``,
  ``"None"``…), anything that is not a single path segment, and anything a
  Firebase uid cannot be (over 128 characters, whitespace, control
  characters).

Everything fails CLOSED with :class:`StorageIdentityUnavailable`, whose
message is French and says nothing was written: a model catches it and
returns it in its error list, a route shows it. There is no fallback —
a file written under the wrong prefix is worse than a file not written.

The two model-reaching imports are LAZY: ``config`` (it resolves the main
service's secrets at import in production, and must never be pulled into a
module that only validates a string) and ``firebase_admin.auth`` (several
test modules stub it as an empty module; a lookup against the stub raises,
which is exactly the fail-closed path). No Firestore.
"""

from __future__ import annotations

import threading
from typing import Optional

from utils.logging_setup import log_unexpected

# A Firebase Auth uid is 1 to 128 characters.
MAX_UID_LENGTH = 128

# What a fallback idiom leaves in a path: the literal the routes used, and
# the strings a None or an undefined value turns into when formatted.
_PLACEHOLDERS = frozenset({"unknown", "none", "null", "undefined", "anonymous"})

UNAVAILABLE_MESSAGE = (
    "L'identité de stockage du cabinet n'a pas pu être établie : aucun "
    "fichier n'a été écrit. Réessayez dans un instant ; si le problème "
    "persiste, reconnectez-vous."
)
INVALID_UID_MESSAGE = (
    "Identité de stockage invalide : aucun fichier n'a été écrit. "
    "Reconnectez-vous, puis réessayez."
)


class StorageIdentityUnavailable(RuntimeError):
    """No trustworthy uid: nothing may be written. The message is French."""

    def __init__(self, message: str = UNAVAILABLE_MESSAGE) -> None:
        super().__init__(message)


class InvalidStorageUid(StorageIdentityUnavailable):
    """A uid was supplied, and it cannot name a Storage prefix."""

    def __init__(self, message: str = INVALID_UID_MESSAGE) -> None:
        super().__init__(message)


_LOCK = threading.Lock()
# {normalized email: uid} — at most one entry: a new email replaces it.
_CACHE: dict[str, str] = {}


def require_uid(uid: object) -> str:
    """Return *uid* if it can name a Storage prefix; raise otherwise."""
    if not isinstance(uid, str) or not uid:
        raise InvalidStorageUid()
    if (
        len(uid) > MAX_UID_LENGTH
        or uid.lower() in _PLACEHOLDERS
        or uid in (".", "..")
        or "/" in uid
        or "\\" in uid
        or any(ch.isspace() or not ch.isprintable() for ch in uid)
    ):
        raise InvalidStorageUid()
    return uid


def _authorized_email() -> str:
    from config import Config  # lazy — see the module docstring

    return str(getattr(Config, "AUTHORIZED_USER_EMAIL", "") or "").strip().lower()


def owner_uid() -> str:
    """The authorized user's Firebase uid — memoized, never a fallback.

    Raises :class:`StorageIdentityUnavailable` when the email is not
    configured, the lookup fails, or it returns a uid :func:`require_uid`
    refuses. A failure is not cached: the next call retries the lookup.
    """
    try:
        email = _authorized_email()
    except Exception as exc:
        log_unexpected("storage identity: configuration unreadable")
        raise StorageIdentityUnavailable() from exc
    if not email:
        raise StorageIdentityUnavailable()
    with _LOCK:
        cached = _CACHE.get(email)
        if cached:
            return cached
        # The lookup runs under the lock on purpose: once per process, one
        # caller asks and the others wait for its answer rather than each
        # repeating it.
        try:
            from firebase_admin import auth as fb_auth  # lazy

            record = fb_auth.get_user_by_email(email)
            uid = getattr(record, "uid", None)
        except Exception as exc:
            # Never the email: the redaction filter would scrub it, but a
            # log line has no business carrying it in the first place.
            log_unexpected("storage identity: owner lookup failed")
            raise StorageIdentityUnavailable() from exc
        try:
            uid = require_uid(uid)
        except InvalidStorageUid as exc:
            log_unexpected(
                "storage identity: owner lookup returned no usable uid",
                exc_info=False,
            )
            raise StorageIdentityUnavailable() from exc
        _CACHE.clear()
        _CACHE[email] = uid
        return uid


def _session_uid() -> Optional[object]:
    try:
        from flask import has_request_context, session
    except Exception:  # pragma: no cover — Flask is a hard dependency
        return None
    if not has_request_context():
        return None
    return session.get("user_id")


def request_uid() -> str:
    """The uid the current request writes under.

    The session's when the request carries one — the uid ``auth.py``
    verified at sign-in, and the prefix every object this session uploaded
    already lives under — validated like any other; :func:`owner_uid`
    otherwise. A session uid that FAILS validation raises: it is never
    silently swapped for the owner's, which would split one session's
    uploads across two prefixes.
    """
    uid = _session_uid()
    if uid is None:
        return owner_uid()
    return require_uid(uid)


def _reset_cache() -> None:
    """Forget the memoized uid (tests)."""
    with _LOCK:
        _CACHE.clear()
