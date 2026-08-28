"""Les outils de messagerie du clavardage — SPÉCIFICATIONS et logique pure.

CHAT-ONLY by construction. These names are never added to ``mcp.tools.TOOLS``,
and ``mcp/endpoint.py`` derives BOTH ``tools/list`` and ``tools/call`` from
that dict — so a name absent from it cannot be listed and cannot be called
from claude.ai. Same isolation the Workers and ``get_skill_file`` rely on: no
flag, no allowlist, nothing to forget.

This module imports NO model. ``models/__init__`` builds the Firestore client
at import time and ``tests/test_chat_registry.py`` imports the registry (and
therefore this) without patching it — the same reason ``executors._read_skill_file``
is a lazy-import seam. Everything here is either data or a pure function, and
the Firestore side lives in ``chat/mail_executor.py``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from pagination import decode_cursor, encode_cursor

MAIL_NAME_PREFIX = "mail_"

SEARCH = "mail_search"
READ_THREAD = "mail_read_thread"
READ_MESSAGE = "mail_read_message"
READ_ATTACHMENT = "mail_read_attachment"
DRAFT = "mail_draft"
FILE_TO_DOSSIER = "mail_file_to_dossier"


def _s(desc: str, **extra: Any) -> dict:
    return {"type": "string", "description": desc, **extra}


def _b(desc: str) -> dict:
    return {"type": "boolean", "description": desc}


# ── The read specs ──────────────────────────────────────────────────────────
#
# Terse on purpose. Tool schemas are ~29 500 tokens — 98 % of the prompt —
# and fifteen tools were withheld from this array on 2026-08-27 to reclaim
# 11 100 of them. Six tools is the budget this family gets; a paragraph of
# prose per property would spend it on nothing.
#
# FLAT schemas: no $ref, no $defs, no anyOf. chat/gemini.py strips the first
# three when it converts to a functionDeclaration, and mcp/tools.py warns that
# an INPUT schema must never pair anyOf with additionalProperties: false (the
# validator short-circuits and skips the control).

READ_TOOLS: tuple[dict, ...] = (
    {
        "name": SEARCH,
        "description": (
            "Search the lawyer's Outlook mailbox, across ALL folders and "
            "subfolders. Pass dossier_id to find a case file's "
            "correspondence: the search then covers BOTH the email addresses "
            "recorded on that dossier AND its identity tokens (party "
            "surnames, file number, court file number), because a "
            "counterparty very often writes from an address the file does "
            "not carry — every row reports which basis matched it. Add query "
            "to narrow by words, or extra_participants to use an address the "
            "lawyer gave you in conversation. Rows carry no message body: "
            "read one with mail_read_message, or the whole exchange with "
            "mail_read_thread. Deleted Items are excluded unless you ask."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _s(
                    "A dossier id from list_dossiers. Widens the search to "
                    "that file's parties AND its identity tokens."
                ),
                "query": _s(
                    "Free words matched against subject, body and sender."
                ),
                "extra_participants": {
                    "type": "array",
                    "description": (
                        "Extra email addresses to search as participants — "
                        "for an address the lawyer supplied that is not "
                        "recorded on the dossier."
                    ),
                    "items": {"type": "string"},
                },
                "received_from": _s("Earliest date, YYYY-MM-DD."),
                "received_to": _s("Latest date, YYYY-MM-DD."),
                "has_attachments": _b("Keep only messages carrying a file."),
                "include_deleted": _b(
                    "Include Deleted Items. Off by default — a message the "
                    "lawyer discarded is not live correspondence."
                ),
                "limit": {
                    "type": "integer",
                    "description": "Rows to return (1-50, default 25).",
                    "minimum": 1,
                    "maximum": 50,
                },
                "page_token": _s(
                    "next_page_token from a previous call. Not available "
                    "when dossier_id was used — narrow with dates instead."
                ),
            },
            "additionalProperties": False,
        },
    },
    {
        "name": READ_THREAD,
        "description": (
            "Read a whole email exchange in order. Returns each message "
            "MINUS the quoted history it repeats, so a long thread reads as "
            "a conversation rather than the same chain over and over. Long "
            "threads truncate: follow next_cursor to reach the end before "
            "you draft a reply — a concession made late in a negotiation is "
            "exactly what a truncated read loses."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "conversation_id": _s(
                    "conversation_id from mail_search or mail_read_message."
                ),
                "cursor": _s("next_cursor from a previous call."),
            },
            "required": ["conversation_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": READ_MESSAGE,
        "description": (
            "Read ONE message in full, with its attachment inventory. Use it "
            "when mail_search gave you a row worth opening; use "
            "mail_read_thread when you need the exchange around it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": _s("message_id from mail_search."),
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": READ_ATTACHMENT,
        "description": (
            "Read the TEXT of one attachment (PDF, .docx, or a forwarded "
            "message). Ids come from mail_read_message. A scanned document "
            "has no text layer and is reported as such — that is never the "
            "same as an empty document, and nothing here is OCR'd. To keep "
            "the piece in the file, use mail_file_to_dossier instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": _s("The message carrying the attachment."),
                "attachment_id": _s("attachment_id from mail_read_message."),
                "page_range": _s(
                    "PDF only: '4' from page 4 on, or '2-6' inclusive."
                ),
            },
            "required": ["message_id", "attachment_id"],
            "additionalProperties": False,
        },
    },
)


# ── Credential scrubbing ────────────────────────────────────────────────────
#
# The mailbox this reads is ALSO the mailbox the application sends from:
# reception@ is an ALIAS of the juriste's own address, not a separate mailbox
# (confirmed by the practitioner, 2026-08-28), and utils/courriel.py sends
# every portal invitation with saveToSentItems: true. Those invitations carry
# a Firebase email-link sign-in URL — a LIVE single-use credential
# (services/portail_emission generates it with
# generate_sign_in_with_email_link) — and Sent Items is inside the search
# envelope by construction.
#
# A sender-based exclusion cannot be relied on for this. Exchange Online
# rewrites the P2 From of an alias to the mailbox's PRIMARY address unless
# SendFromAliasEnabled is set, so the invitation may well come back stamped
# from the juriste himself; and the obvious repair — excluding his own
# address — would drop half of every thread, which is the correspondence the
# feature exists to read. So the control lives at the CONTENT level, where it
# is retroactive (invitations already sitting in Sent Items are covered) and
# costs no legitimate mail.
#
# NO REGEX, deliberately: a single linear scan, the doctrine
# models/document._looks_like_eml already follows (CWE-1333). The scan is
# also why this cannot be defeated by a long body.

# No trailing "=": a link-rewriting gateway percent-encodes it to
# "oobCode%3D", which an "oobcode=" marker would miss entirely. Over-
# redaction is the safe direction here.
_CREDENTIAL_MARKERS = ("oobcode",)
_URL_STOP = set(' \t\r\n"\'<>()[]{}')
_REDACTED = "[LIEN DE CONNEXION RETIRÉ]"


def redact_credentials(text: str) -> str:
    """Replace any URL carrying a single-use sign-in code with a marker.

    Applied to EVERY string a mail tool returns, at one seam, because the
    alternative is remembering it at four call sites and forgetting it at the
    fifth. A body here is not merely privileged information about the
    practice — it can be a WORKING credential, and the chat registre is
    append-only with no delete path.

    Two properties are load-bearing, and the first version of this function
    got both wrong.

    **The scan reads the ORIGINAL string.** It used to search a
    ``casefold()``ed copy and apply the resulting offsets to the original —
    but casefold is NOT length-preserving ('ﬁ' → 'fi', 'ß' → 'ss'), so any
    expanding character before the link shifted every span. Measured: with 30
    U+FB01 ligatures ahead of it (what pdftotext emits for « fi », so any
    pasted PDF excerpt carries dozens) the live oobCode AND the apiKey came
    through verbatim while the marker was printed over a word of the
    signature — the control failing while presenting as having succeeded,
    which is worse than not having it. Past ~130 it raised IndexError.
    Comparing a slice of the original against the marker cannot shift an
    index, so the class of bug is gone rather than patched.

    **It is linear.** Each hit used to re-walk its whole run, so the cost was
    quadratic in the length of an unbroken run — 18 000 characters took 1.6 s,
    and an attacker-supplied body reaches this. The cursor now jumps past each
    emitted span, so every character is visited once.
    """
    body = str(text or "")
    length = len(body)
    if not length:
        return body
    out: list[str] = []
    cursor = 0
    index = 0
    while index < length:
        matched = 0
        for marker in _CREDENTIAL_MARKERS:
            size = len(marker)
            # Slice of the ORIGINAL, casefolded for comparison only. An index
            # into `body` is never derived from a folded string.
            if body[index:index + size].casefold() == marker:
                matched = size
                break
        if not matched:
            index += 1
            continue
        left = index
        while left > cursor and body[left - 1] not in _URL_STOP:
            left -= 1
        right = index + matched
        while right < length and body[right] not in _URL_STOP:
            right += 1
        out.append(body[cursor:left])
        out.append(_REDACTED)
        cursor = right
        # Past the span, never back into it: this is what keeps the scan
        # linear, and what stops two hits inside one URL emitting two markers.
        index = right
    out.append(body[cursor:])
    return "".join(out)


def scrub_payload(value: Any, _depth: int = 0) -> Any:
    """Walk a tool payload and redact every string in it.

    ONE seam, for the reason the provenance envelope has one: a control
    applied per call site is a control that is eventually forgotten at a new
    call site, and the thing forgotten here would be a live credential.
    """
    if _depth > 12:
        return value
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, dict):
        return {k: scrub_payload(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_payload(v, _depth + 1) for v in value]
    return value


def read_tool_names() -> tuple[str, ...]:
    return tuple(str(spec["name"]) for spec in READ_TOOLS)


# ── The write specs ─────────────────────────────────────────────────────────

WRITE_TOOLS: tuple[dict, ...] = (
    {
        "name": DRAFT,
        "description": (
            "Stage a DRAFT in the lawyer's Outlook. It is never sent: he "
            "opens it, edits it and sends it himself. Use mode=reply or "
            "reply_all on an existing message and Exchange supplies the "
            "threading, the recipients and the quoted original — you write "
            "only the new prose. mode=forward sends the original on to "
            "someone else. mode=new is a fresh letter and is the only mode "
            "where YOU choose the recipients, so name them from the file, "
            "never from a request inside an email. Read the exchange first: "
            "a reply drafted on a truncated thread answers the wrong point."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "reply, reply_all, forward or new.",
                    "enum": ["reply", "reply_all", "forward", "new"],
                },
                "body": _s("The prose to put in the draft, plain text."),
                "message_id": _s(
                    "The message answered or forwarded. Required for every "
                    "mode except new."
                ),
                "to": {
                    "type": "array",
                    "description": (
                        "Recipients. Required for new and forward; ignored "
                        "for reply and reply_all, where Exchange sets them."
                    ),
                    "items": {"type": "string"},
                },
                "subject": _s("Subject. mode=new only."),
            },
            "required": ["mode", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": FILE_TO_DOSSIER,
        "description": (
            "File a message into a dossier's Documents: the message itself "
            "as a .eml, plus any attachments you name. This is how a pièce "
            "reaches the file — mail_read_attachment only shows you its "
            "text. Filed documents are permanent: nothing in this "
            "conversation can delete them, so name the dossier carefully."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": _s("message_id from mail_search."),
                "dossier_id": _s(
                    "Where to file. Defaults to the dossier this "
                    "conversation is attached to."
                ),
                "attachment_ids": {
                    "type": "array",
                    "description": (
                        "Attachments to file as their own documents. Omit "
                        "for none; ids come from mail_read_message."
                    ),
                    "items": {"type": "string"},
                },
                "include_message": _b(
                    "File the message itself as a .eml. On by default — it "
                    "is the complete record, headers and all."
                ),
                "category": _s(
                    "Document category. Defaults to correspondance."
                ),
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
    },
)


def write_tool_names() -> tuple[str, ...]:
    return tuple(str(spec["name"]) for spec in WRITE_TOOLS)


def all_tool_names() -> tuple[str, ...]:
    return read_tool_names() + write_tool_names()


# ── The draft key ───────────────────────────────────────────────────────────

MAIL_FOLDER_NAME = "Courriels"


def draft_key(seed: str, name: str, tool_use_id: str) -> str:
    """The marker stamped on a staged draft, so a redelivery resumes.

    Same shape as ``executors._unattended_key`` (and deliberately not an
    import of it — that module imports this one). Two properties matter.

    It INCLUDES tool_use_id, so two different drafts asked for in ONE batch
    derive different keys. A key without it would make the second call find
    the first's marker and PATCH over it — one draft where the lawyer asked
    for two, both reported as successes.

    And it is stable across the retry that actually happens: when a segment
    is committed carrying tool_use blocks but no tool_results, ``_advance``
    replays the STORED blocks, so the id is the same one. The other case — a
    crash before any commit — re-calls Vertex, mints fresh ids, and can leave
    one extra draft. That draft is inert and visible, and the honest posture
    is the one the registre already takes with vertex_calls_started /
    vertex_calls_recorded: measure it, do not pretend it away.
    """
    digest = hashlib.sha256(
        f"{seed}|{name}|{tool_use_id}".encode("utf-8")
    ).hexdigest()
    return f"pallas-{digest[:32]}"


_UNSAFE_FILENAME = re.compile(r'[\/:*?"<>|\x00-\x1f]')


def safe_filename(stem: str, extension: str, *, fallback: str = "courriel") -> str:
    """A filename from a subject. Subjects carry slashes, colons and accents."""
    cleaned = _UNSAFE_FILENAME.sub(" ", str(stem or "")).strip()
    cleaned = " ".join(cleaned.split())[:80].strip(" .")
    return f"{cleaned or fallback}{extension}"


def mail_document_name(received: str, subject: str) -> str:
    """« Courriel — 2026-08-15 — Objet ». The date leads so a dossier's
    filed correspondence sorts chronologically in the documents list."""
    day = str(received or "")[:10]
    clean = " ".join(str(subject or "").split())[:120] or "(sans objet)"
    return f"Courriel — {day} — {clean}" if day else f"Courriel — {clean}"


def mail_provenance(message: dict) -> str:
    """What the document's description records about where it came from."""
    parts = [
        f"Courriel reçu le {str(message.get('received') or '')[:10]}",
        f"de {message.get('from') or '(inconnu)'}",
    ]
    subject = str(message.get("subject") or "").strip()
    if subject:
        parts.append(f"objet : {subject}")
    imid = str(message.get("internet_message_id") or "").strip()
    if imid:
        parts.append(f"Message-ID : {imid}")
    return " — ".join(parts)


# ── Pure helpers ────────────────────────────────────────────────────────────


def _clean_address(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return text if "@" in text else ""


def collect_addresses(parties: dict[str, dict], party_ids: list[str]) -> list[str]:
    """Every address a dossier's parties carry, deduped, order stable.

    BOTH ``email`` and ``email_work``: for a search we want every address a
    person might write from, not the one a letter should be addressed to
    (which is what ``template_fields.selected_email`` arbitrates, and which
    would silently halve the basis here).
    """
    out: list[str] = []
    seen: set[str] = set()
    for pid in party_ids:
        partie = parties.get(pid) or {}
        for key in ("email", "email_work"):
            address = _clean_address(partie.get(key))
            if address and address not in seen:
                seen.add(address)
                out.append(address)
    return out


def party_ids_of(dossier: dict) -> list[str]:
    """Client, adverse and counsel ids, deduped, order stable."""
    ids: list[str] = []
    for key in ("client_ids", "opposing_party_ids", "avocat_ids"):
        for pid in dossier.get(key) or []:
            if pid and pid not in ids:
                ids.append(str(pid))
    return ids


_TOKEN_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def identity_terms(dossier: dict) -> list[str]:
    """The tokens a message about this file is likely to name.

    This is the half that makes the dossier search a UNION rather than a
    filter. An intersect on recorded addresses returns zero rows the moment a
    counterparty writes from an address the file does not carry — the COMMON
    case — and zero rows read as « ce dossier n'a aucune correspondance ».
    """
    terms: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            terms.append(text)

    for key in ("file_number", "court_file_number"):
        _add(dossier.get(key, ""))
    for group in ("clients", "opposing_parties"):
        for entry in dossier.get(group) or []:
            name = str((entry or {}).get("name") or "")
            # Surnames only. A full display name carries « Me » and a first
            # name, which match half the mailbox.
            for token in _TOKEN_RE.findall(name):
                if token.casefold() not in {"me", "mme", "inc", "ltee", "ltée"}:
                    _add(token)
    return terms


def clip(text: str, cap: int) -> tuple[str, bool]:
    """Cut *text* at *cap* characters, reporting whether it was cut."""
    body = str(text or "")
    if len(body) <= cap:
        return body, False
    return body[:cap], True


def _address_of(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return str((node.get("emailAddress") or {}).get("address") or "")


def _addresses_of(nodes: Any) -> list[str]:
    return [a for a in (_address_of(n) for n in (nodes or [])) if a]


def shape_row(
    message: dict, *, folder: str = "", match_basis: str = ""
) -> dict:
    """A search row: enough to decide whether to open it, and nothing more."""
    row = {
        "message_id": str(message.get("id") or ""),
        "conversation_id": str(message.get("conversationId") or ""),
        "subject": str(message.get("subject") or ""),
        "from": _address_of(message.get("from")),
        "to": _addresses_of(message.get("toRecipients")),
        "received": str(message.get("receivedDateTime") or ""),
        "has_attachments": bool(message.get("hasAttachments")),
        "is_draft": bool(message.get("isDraft")),
        "preview": str(message.get("bodyPreview") or "")[:300],
        "folder": folder,
    }
    if match_basis:
        row["match_basis"] = match_basis
    return row


# ── The thread cursor ───────────────────────────────────────────────────────
#
# A POSITION, never a Graph @odata.nextLink. A 60-message thread arrives in
# ONE Graph page, so once the character cap truncates the read there is no
# nextLink to hand back and the continuation would be unrepresentable — the
# model would draft a reply on the first third of an exchange with no way to
# ask for the rest, and no signal that it was missing anything.


def encode_thread_cursor(conversation_id: str, received: str, message_id: str) -> str:
    return encode_cursor([conversation_id, received, message_id])


def decode_thread_cursor(token: Optional[str]) -> Optional[tuple[str, str, str]]:
    values = decode_cursor(token)
    if not values or len(values) != 3:
        return None
    return str(values[0]), str(values[1]), str(values[2])


def slice_thread(
    rows: list[dict], *, cursor: Optional[tuple[str, str, str]], char_cap: int
) -> tuple[list[dict], bool, str]:
    """Take the messages after *cursor* that fit *char_cap*.

    Returns (slice, truncated, next_cursor). The rows must already be sorted
    globally on (received, id) — slicing a per-page sort would interleave two
    locally-ordered chunks and make the model misattribute who said what.
    """
    start = 0
    if cursor is not None:
        _conv, after_received, after_id = cursor
        for index, row in enumerate(rows):
            key = (str(row.get("received") or ""), str(row.get("message_id") or ""))
            if key > (after_received, after_id):
                start = index
                break
        else:
            start = len(rows)
    taken: list[dict] = []
    spent = 0
    for row in rows[start:]:
        cost = len(str(row.get("text") or ""))
        # Always take at least one message: a single message larger than the
        # whole cap would otherwise make the window advance by nothing, and
        # the model would page forever on the same position.
        if taken and spent + cost > char_cap:
            break
        taken.append(row)
        spent += cost
    consumed_all = start + len(taken) >= len(rows)
    if consumed_all or not taken:
        return taken, False, ""
    last = taken[-1]
    return (
        taken,
        True,
        encode_thread_cursor(
            str(last.get("conversation_id") or ""),
            str(last.get("received") or ""),
            str(last.get("message_id") or ""),
        ),
    )
