"""La boîte de courriels du juriste — lecture bornée et brouillons (2026-08-28).

The mailbox sibling of ``graph_calendrier`` (what Bookings reads) and
``graph_miroir`` (what the mirror writes). The name is deliberate: « courriel »
is a message the application SENDS (``utils/courriel.py``, and
``tests/test_graph_courriel.py`` already owns that name), « messagerie » is the
mailbox it READS. Opposite directions, and colliding the names would put two
unrelated subsystems under one test file.

Firestore-free and Flask-free, like both siblings. Everything here is testable
without a harness; the dossier side of the feature (linking a message to a
file, versing an attachment into Documents) belongs to the executor layer,
never here.

MAIN/CHAT SERVICE ONLY — the portal's environment carries no Graph credential,
so any call from that process raises ``GraphNotConfigured``.

Three rules this module exists to enforce:

* **It never calls ``graph.graph_get``.** That verb merges EVERY nextLink page
  with no cap of any kind. Safe for a 90-day calendar window; catastrophic for
  a mailbox, where one query walks thousands of messages and every page
  carries bodies. Pinned by a source sweep in the tests.
* **Every attachment listing sends a ``$select``.** Without one Graph inlines
  each attachment's ``contentBytes`` as base64 — a 24 MiB lot becomes ~64 MiB
  of transient peak on a 512 MB instance, unbounded.
* **Every interpolated id is percent-encoded.** Outlook message ids are
  base64-ish and routinely contain ``/``, ``+`` and ``=``; an unescaped ``/``
  silently addresses a DIFFERENT resource rather than failing.
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from typing import Any, Optional
from urllib.parse import quote

from config import Config
from utils import graph
from utils.graph import GraphError, GraphNotConfigured, GraphTooLarge

__all__ = [
    "MailBudgetExhausted",
    "MailRefused",
    "MARKER_GUID",
    "MARKER_PROP_ID",
    "MARKER_CATEGORY",
    "EXPAND_MARKER",
    "start_budget",
    "reset_budget",
    "reset_caches_for_tests",
    "build_kql",
    "folder_path",
    "deleted_items_id",
    "search_messages",
    "list_conversation",
    "get_message",
    "list_attachments",
    "get_attachment_bytes",
    "get_message_mime",
    "create_anchored_draft",
    "create_new_draft",
    "set_draft_body",
    "list_marked_drafts",
    "marker_of",
]


class MailBudgetExhausted(GraphError):
    """The turn's shared mail budget ran out before this call could start."""


class MailRefused(GraphError):
    """A caller-side refusal (a value this module will not send to Graph)."""


# ── The marker on a staged draft ────────────────────────────────────────────
#
# The graph_miroir pattern, transposed. The GUID is FROZEN FOREVER: changing
# it orphans every draft already staged — invisible to the duplicate check,
# and therefore silently duplicating from that deploy onward.
#
# The category is the HUMAN half. The lawyer opening Outlook must be able to
# tell at a glance which drafts the assistant wrote, without opening them.
MARKER_GUID = "6f2b9d41-8a3e-4f57-9c10-2d6ba7e4f813"
MARKER_PROP_ID = f"String {{{MARKER_GUID}}} Name PallasAthenaDraftKey"
MARKER_CATEGORY = "Pallas Athéna"
EXPAND_MARKER = (
    f"singleValueExtendedProperties($filter=id eq '{MARKER_PROP_ID}')"
)

# The message fields a listing needs. Note what is ABSENT: body and
# uniqueBody. A search returns rows, not correspondence — the model reads a
# body only by naming a message.
_ROW_SELECT = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,sentDateTime,hasAttachments,isDraft,isRead,"
    "parentFolderId,webLink,bodyPreview"
)
# Adds the body pair. uniqueBody is the message MINUS quoted history, which is
# what makes a thread readable: without it the same quoted chain repeats once
# per message and a 60-message thread is mostly duplication.
_FULL_SELECT = _ROW_SELECT + ",body,uniqueBody,internetMessageId"
# Metadata only — contentBytes deliberately excluded (see the module docstring).
_ATTACHMENT_SELECT = "id,name,contentType,size,isInline,lastModifiedDateTime"

# Plain text instead of HTML. Graph returns HTML unless asked, and HTML bodies
# are several times the tokens for the same words.
_PREFER_TEXT = {"Prefer": 'outlook.body-content-type="text"'}

_RETRYABLE_READ = frozenset({429, 503, 504})
# A write retries 429 ONLY. A 5xx on a POST is ambiguous — Graph may have
# created the draft and failed to answer — and retrying it is exactly the
# duplicate-draft hazard the no-gating decision creates.
_RETRYABLE_WRITE = frozenset({429})


# ── The turn-scoped budget ──────────────────────────────────────────────────
#
# ONE budget for the whole family, not one per call. The tool phase runs in
# the SAME gunicorn request as the Vertex call it follows (chat.yaml sets
# --timeout 570), and turn_engine._run_tools iterates its batch with no length
# check, so per-call ceilings do not compose into a per-request bound.
#
# A ContextVar rather than a threaded parameter: the same seam chat_draft uses
# for provenance, and it keeps every signature here free of plumbing.
_DEADLINE: ContextVar[Optional[float]] = ContextVar("mail_deadline", default=None)


def start_budget(seconds: Optional[float] = None):
    """Open the turn's mail budget; returns a token for :func:`reset_budget`."""
    span = float(Config.CHAT_MAIL_TURN_BUDGET_S if seconds is None else seconds)
    return _DEADLINE.set(time.monotonic() + span)


def reset_budget(token) -> None:
    _DEADLINE.reset(token)


def _remaining() -> Optional[float]:
    deadline = _DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()


def _check_budget() -> None:
    left = _remaining()
    if left is not None and left <= 0:
        raise MailBudgetExhausted(
            "Le temps alloué aux appels de messagerie pour ce tour est "
            "épuisé. Reprenez au tour suivant."
        )


def _timeout() -> int:
    """The per-request timeout, never larger than the budget that remains."""
    configured = int(Config.CHAT_MAIL_HTTP_TIMEOUT_S)
    left = _remaining()
    if left is None:
        return configured
    return max(1, min(configured, int(left)))


def _call(fn, *, write: bool):
    """Run a Graph call with a bounded, method-aware retry."""
    attempts = max(1, int(Config.CHAT_MAIL_RETRY_MAX_ATTEMPTS))
    retryable = _RETRYABLE_WRITE if write else _RETRYABLE_READ
    for attempt in range(attempts):
        _check_budget()
        try:
            return fn()
        except GraphTooLarge:
            raise  # a ceiling, never a transient
        except GraphError as exc:
            status = getattr(exc, "status", None)
            if status not in retryable or attempt == attempts - 1:
                raise
            hinted = getattr(exc, "retry_after_s", None)
            pause = min(
                float(hinted if hinted is not None else 2 ** attempt),
                float(Config.CHAT_MAIL_RETRY_MAX_SLEEP_S),
            )
            left = _remaining()
            if left is not None and pause >= left:
                raise
            time.sleep(pause)
    raise AssertionError("unreachable")  # pragma: no cover


# ── Addressing ──────────────────────────────────────────────────────────────


def _q(value: str) -> str:
    """Percent-encode a path segment. safe="" is the point: an Outlook id
    contains / and + routinely, and an unescaped / addresses a different
    resource rather than failing."""
    return quote(str(value), safe="")


def _mailbox() -> str:
    upn = (Config.CHAT_MAIL_UPN or "").strip()
    if not upn:
        raise GraphNotConfigured(
            "La boîte de courriels de l'assistant n'est pas configurée."
        )
    return f"/users/{_q(upn)}"


# ── KQL ─────────────────────────────────────────────────────────────────────


def _kql_safe(value: str) -> str:
    """A KQL clause value. Refused rather than escaped when it carries a
    delimiter: a quote inside the clause would silently change the query's
    shape, and a search that quietly means something else is worse on a
    privileged corpus than one that refuses."""
    text = str(value).strip()
    if '"' in text or "\\" in text:
        raise MailRefused(
            "Valeur de recherche refusée : les guillemets et les barres "
            "obliques inverses ne peuvent pas figurer dans une clause."
        )
    return text


def build_kql(
    *,
    participants: tuple[str, ...] = (),
    terms: tuple[str, ...] = (),
    received_from: str = "",
    received_to: str = "",
    has_attachments: Optional[bool] = None,
) -> str:
    """Assemble one KQL query string (WITHOUT the enclosing quotes).

    Participants are OR'd together and the group is AND'ed with the terms —
    « anyone on this file » narrowed by « about this ». Free terms are OR'd,
    because a dossier's identity tokens (a surname, a file number) are
    alternatives, not conjuncts.
    """
    if len(participants) > int(Config.CHAT_MAIL_MAX_ADDRESSES):
        # REFUSED, never truncated: a dropped participant is a dropped party,
        # and the caller cannot see the loss in the results.
        raise MailRefused(
            f"Trop d'adresses ({len(participants)}) : la recherche en accepte "
            f"au plus {Config.CHAT_MAIL_MAX_ADDRESSES}. Restreignez la liste."
        )
    groups: list[str] = []
    if participants:
        groups.append(
            "("
            + " OR ".join(f"participants:{_kql_safe(a)}" for a in participants if str(a).strip())
            + ")"
        )
    cleaned_terms = [_kql_safe(t) for t in terms if str(t).strip()]
    if cleaned_terms:
        groups.append("(" + " OR ".join(cleaned_terms) + ")")
    if received_from or received_to:
        start = _kql_safe(received_from) or "1900-01-01"
        end = _kql_safe(received_to) or "2999-12-31"
        groups.append(f"received:{start}..{end}")
    if has_attachments is not None:
        groups.append(f"hasAttachments:{'true' if has_attachments else 'false'}")
    return " AND ".join(g for g in groups if g and g != "()")


# ── Folder paths ────────────────────────────────────────────────────────────
#
# A LABEL, resolved lazily per message, not an index build: the mailbox spans
# every folder without our knowing any folder's name, so the path is for the
# lawyer's eyes. Only the Deleted Items id is ever load-bearing, and it is one
# well-known alias rather than a tree that may be incomplete.
#
# The posture SPLITS accordingly: labelling may fail open (an empty path is a
# missing display string), EXCLUSION may not (claiming the Corbeille was
# excluded when the lookup failed would present discarded mail as live
# correspondence). Callers read ``ok`` before asserting anything.

_folder_lock = threading.Lock()
_folder_cache: dict[str, tuple[str, str]] = {}   # id -> (displayName, parentId)
_folder_cache_at: float = 0.0
_deleted_id: Optional[str] = None
_deleted_probed_at: float = 0.0


def reset_caches_for_tests() -> None:
    global _folder_cache, _folder_cache_at, _deleted_id, _deleted_probed_at
    with _folder_lock:
        _folder_cache = {}
        _folder_cache_at = 0.0
        _deleted_id = None
        _deleted_probed_at = 0.0


def _cache_fresh(stamp: float) -> bool:
    return bool(stamp) and (time.monotonic() - stamp) < float(
        Config.CHAT_MAIL_FOLDER_CACHE_TTL_S
    )


def deleted_items_id() -> tuple[Optional[str], bool]:
    """The Deleted Items folder id, and whether the lookup SUCCEEDED.

    The second member is the whole point. ``(None, True)`` cannot happen for a
    real mailbox; ``(None, False)`` means we do not know, and a caller must
    then not claim the Corbeille was excluded.
    """
    global _deleted_id, _deleted_probed_at
    with _folder_lock:
        if _cache_fresh(_deleted_probed_at) and _deleted_id:
            return _deleted_id, True
    try:
        data = _call(
            lambda: graph.graph_get_page(
                f"{_mailbox()}/mailFolders/deleteditems",
                {"$select": "id,displayName"},
                timeout=_timeout(),
            ),
            write=False,
        )
    except GraphError:
        return None, False
    folder_id = str(data.get("id") or "")
    if not folder_id:
        return None, False
    with _folder_lock:
        _deleted_id = folder_id
        _deleted_probed_at = time.monotonic()
    return folder_id, True


def _folder_entry(folder_id: str) -> Optional[tuple[str, str]]:
    global _folder_cache, _folder_cache_at
    with _folder_lock:
        if not _cache_fresh(_folder_cache_at):
            _folder_cache = {}
            _folder_cache_at = time.monotonic()
        hit = _folder_cache.get(folder_id)
    if hit is not None:
        return hit
    try:
        data = _call(
            lambda: graph.graph_get_page(
                f"{_mailbox()}/mailFolders/{_q(folder_id)}",
                {"$select": "id,displayName,parentFolderId"},
                timeout=_timeout(),
            ),
            write=False,
        )
    except GraphError:
        return None
    entry = (
        str(data.get("displayName") or ""),
        str(data.get("parentFolderId") or ""),
    )
    with _folder_lock:
        _folder_cache[folder_id] = entry
    return entry


def folder_path(folder_id: str) -> tuple[str, bool]:
    """A readable path for *folder_id*, and whether it RESOLVED completely.

    Walks parents to the root, bounded by CHAT_MAIL_FOLDER_MAX_DEPTH. The
    boolean is the honesty half: a partial walk still returns what it has, and
    says so.
    """
    if not folder_id:
        return "", False
    names: list[str] = []
    current = folder_id
    for _ in range(int(Config.CHAT_MAIL_FOLDER_MAX_DEPTH)):
        entry = _folder_entry(current)
        if entry is None:
            return "/".join(reversed(names)), False
        name, parent = entry
        if name:
            names.append(name)
        if not parent:
            return "/".join(reversed(names)), True
        current = parent
    return "/".join(reversed(names)), False


# ── Reads ───────────────────────────────────────────────────────────────────


def search_messages(
    *,
    kql: str = "",
    received_from: str = "",
    top: Optional[int] = None,
    page_url: str = "",
) -> tuple[list[dict], str]:
    """One page of messages. Returns (rows, next_page_url).

    Two mutually exclusive query shapes, because Graph does not accept them
    together on messages: a ``$search`` (which is already sorted by date and
    rejects ``$orderby``), or a ``$filter``/``$orderby`` pair. Mixing them is
    undocumented for messages and is not attempted.
    """
    page_size = int(top or Config.CHAT_MAIL_LIST_PAGE_SIZE)
    if page_url:
        data = _call(
            lambda: graph.graph_get_page(
                page_url, extra_headers=_PREFER_TEXT, timeout=_timeout()
            ),
            write=False,
        )
    else:
        params: dict[str, Any] = {"$select": _ROW_SELECT, "$top": page_size}
        if kql:
            params["$search"] = f'"{kql}"'
        else:
            # No search terms: the legal pairing is a filter whose property
            # also leads the orderby (else Graph answers InefficientFilter).
            params["$orderby"] = "receivedDateTime desc"
            if received_from:
                params["$filter"] = f"receivedDateTime ge {received_from}T00:00:00Z"
        data = _call(
            lambda: graph.graph_get_page(
                f"{_mailbox()}/messages",
                params,
                extra_headers=_PREFER_TEXT,
                timeout=_timeout(),
            ),
            write=False,
        )
    rows = [r for r in (data.get("value") or []) if isinstance(r, dict)]
    return rows, str(data.get("@odata.nextLink") or "")


def list_conversation(conversation_id: str, *, top: Optional[int] = None) -> list[dict]:
    """Every message of one thread, chronological.

    ``$filter`` on conversationId with NO ``$orderby``: the InefficientFilter
    rule demands that an ordered property lead the filter, and sorting a
    bounded thread in Python is both simpler and correct across page seams —
    per-page sorting would interleave two locally-sorted chunks and make the
    model misattribute who said what when.
    """
    safe = _kql_safe(conversation_id).replace("'", "''")
    params = {
        "$select": _FULL_SELECT,
        "$top": int(top or 100),
        "$filter": f"conversationId eq '{safe}'",
    }
    data = _call(
        lambda: graph.graph_get_page(
            f"{_mailbox()}/messages",
            params,
            extra_headers=_PREFER_TEXT,
            timeout=_timeout(),
        ),
        write=False,
    )
    rows = [r for r in (data.get("value") or []) if isinstance(r, dict)]
    rows.sort(key=lambda r: (str(r.get("receivedDateTime") or ""), str(r.get("id") or "")))
    return rows


def get_message(message_id: str) -> dict:
    """One message WITH its bodies, in plain text."""
    return _call(
        lambda: graph.graph_get_page(
            f"{_mailbox()}/messages/{_q(message_id)}",
            {"$select": _FULL_SELECT},
            extra_headers=_PREFER_TEXT,
            timeout=_timeout(),
        ),
        write=False,
    )


def list_attachments(message_id: str) -> list[dict]:
    """Attachment METADATA. The $select is the bound — see the module doc."""
    data = _call(
        lambda: graph.graph_get_page(
            f"{_mailbox()}/messages/{_q(message_id)}/attachments",
            {"$select": _ATTACHMENT_SELECT},
            timeout=_timeout(),
        ),
        write=False,
    )
    return [r for r in (data.get("value") or []) if isinstance(r, dict)]


def get_attachment_bytes(
    message_id: str, attachment_id: str, *, max_bytes: Optional[int] = None
) -> tuple[bytes, str]:
    """One attachment's raw bytes.

    ``$value`` rather than the base64 ``contentBytes`` of the JSON form: the
    bytes then stream under a ceiling instead of arriving inside a JSON
    document that must be held whole. For an itemAttachment (a forwarded
    message) it returns MIME, which is exactly the .eml we want.
    """
    cap = int(max_bytes or Config.CHAT_MAIL_ATTACHMENT_MAX_BYTES)
    return _call(
        lambda: graph.graph_get_bytes(
            f"{_mailbox()}/messages/{_q(message_id)}"
            f"/attachments/{_q(attachment_id)}/$value",
            max_bytes=cap,
            timeout=_timeout(),
        ),
        write=False,
    )


def get_message_mime(message_id: str, *, max_bytes: Optional[int] = None) -> bytes:
    """The whole message as RFC-822 MIME — the .eml, attachments included."""
    cap = int(max_bytes or Config.CHAT_MAIL_EML_MAX_BYTES)
    payload, _ = _call(
        lambda: graph.graph_get_bytes(
            f"{_mailbox()}/messages/{_q(message_id)}/$value",
            max_bytes=cap,
            timeout=_timeout(),
        ),
        write=False,
    )
    return payload


# ── Drafts ──────────────────────────────────────────────────────────────────
#
# There is no send verb in this module, and none may be added. A draft is
# created, its body is set, and it stops there.

_ANCHORED_MODES = {
    "reply": "createReply",
    "reply_all": "createReplyAll",
    "forward": "createForward",
}


def create_anchored_draft(
    message_id: str, mode: str, *, to: tuple[str, ...] = ()
) -> dict:
    """createReply / createReplyAll / createForward → a draft.

    Anchored to a real message, so threading, recipients and the quoted
    original all come from Exchange rather than from the model.
    """
    action = _ANCHORED_MODES.get(mode)
    if action is None:
        raise MailRefused(f"Mode de brouillon inconnu : « {mode} ».")
    # createForward with no recipients yields a draft addressed to nobody,
    # which reads as a silent failure the moment the lawyer opens it.
    payload: dict[str, Any] = {}
    if to:
        payload["toRecipients"] = [{"emailAddress": {"address": a}} for a in to]
    body, _status = _call(
        lambda: graph.graph_send(
            "POST",
            f"{_mailbox()}/messages/{_q(message_id)}/{action}",
            json_body=payload,
            # Without it the quoted thread is stamped in UTC and the lawyer
            # reads « 14:00 » for a 10:00 Montréal exchange.
            extra_headers={"Prefer": 'outlook.timezone="Eastern Standard Time"'},
            timeout=_timeout(),
        ),
        write=True,
    )
    return body or {}


def create_new_draft(
    *, to: tuple[str, ...], subject: str, body_text: str,
    cc: tuple[str, ...] = (), marker: str = "",
) -> dict:
    """A standalone draft. Recipients come from the MODEL here, which is why
    this is the one draft shape with no Exchange-supplied anchor."""
    payload: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body_text},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "categories": [MARKER_CATEGORY],
    }
    if cc:
        payload["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    if marker:
        payload["singleValueExtendedProperties"] = [
            {"id": MARKER_PROP_ID, "value": marker}
        ]
    body, _status = _call(
        lambda: graph.graph_send(
            "POST", f"{_mailbox()}/messages", json_body=payload,
            timeout=_timeout(),
        ),
        write=True,
    )
    return body or {}


def set_draft_body(draft_id: str, body_text: str, *, marker: str = "") -> dict:
    """PATCH a draft's body (and stamp its marker on the same request).

    One request rather than two: a crash between them would leave an
    unmarked draft that the duplicate check can never find again.
    """
    payload: dict[str, Any] = {
        "body": {"contentType": "Text", "content": body_text},
        "categories": [MARKER_CATEGORY],
    }
    if marker:
        payload["singleValueExtendedProperties"] = [
            {"id": MARKER_PROP_ID, "value": marker}
        ]
    body, _status = _call(
        lambda: graph.graph_send(
            "PATCH", f"{_mailbox()}/messages/{_q(draft_id)}",
            json_body=payload, timeout=_timeout(),
        ),
        write=True,
    )
    return body or {}


def marker_of(message: dict) -> str:
    """The draft key stamped on a message, or "" — read CLIENT-SIDE.

    A server-side $filter existence test on an extended property is
    unreliable in Graph (the finding graph_miroir already carries), so the
    property is expanded and matched here.
    """
    for prop in message.get("singleValueExtendedProperties") or []:
        if isinstance(prop, dict) and prop.get("id") == MARKER_PROP_ID:
            return str(prop.get("value") or "")
    return ""


def list_marked_drafts(*, top: int = 50) -> list[dict]:
    """The most recent drafts, with their marker expanded.

    ``$orderby=createdDateTime desc`` is load-bearing and it is LEGAL here
    (no $search, no $filter on this request, so the InefficientFilter rule
    does not apply). Without it the page is whatever Graph returns, and the
    duplicate check silently stops finding markers once the lawyer's Drafts
    folder outgrows one page — reopening the exact hazard it exists to close.
    """
    data = _call(
        lambda: graph.graph_get_page(
            f"{_mailbox()}/mailFolders/drafts/messages",
            {
                "$select": "id,subject,conversationId,createdDateTime,webLink",
                "$orderby": "createdDateTime desc",
                "$top": int(top),
                "$expand": EXPAND_MARKER,
            },
            timeout=_timeout(),
        ),
        write=False,
    )
    return [r for r in (data.get("value") or []) if isinstance(r, dict)]
