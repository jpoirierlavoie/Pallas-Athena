"""L'exécution des outils de messagerie (lot 2026-08-28).

The Firestore-and-Graph half of the mail family; ``chat/mail_tools.py`` holds
the specs and every pure function. Models are imported LAZILY inside the
functions that need them, for the reason ``executors._read_skill_file``
documents: ``models/__init__`` builds the Firestore client at import, and the
registry (which reaches this module's specs) is imported by tests that do not
patch it.

Nothing here raises out of a turn. Every refusal is a French message returned
as an error tool_result, so the model can correct itself and the chain
continues — the ``chat/executors.py`` contract, inherited.
"""

from __future__ import annotations

import email
import io
from email import policy
from typing import Any, Optional

from config import Config
from utils import graph_messagerie as gm
from utils import pdf_text
from utils.graph import GraphError, GraphNotConfigured, GraphTooLarge
from utils.logging_setup import log_unexpected

from chat import mail_tools

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Machine-stable reason -> French sentence, the get_document_text pattern.
# The token is for the model, the sentence is for the lawyer, and they travel
# together so neither has to be reconstructed from the other.
_UNREADABLE_FR = {
    "type_non_extractible": (
        "Cette pièce n'a pas de couche de texte lisible ici (type {type}). "
        "Versez-la au dossier pour la consulter dans l'application."
    ),
    "piece_trop_volumineuse": (
        "Pièce trop volumineuse pour être lue dans une conversation "
        "({size} octets). Versez-la au dossier."
    ),
    "lien_infonuagique": (
        "Cette pièce est un lien vers un fichier infonuagique, pas un "
        "fichier joint : son contenu n'est pas dans le courriel."
    ),
    "chiffre": "Cette pièce est protégée par un mot de passe.",
    "invalid_pdf": "Ce PDF est illisible (fichier endommagé).",
    "invalid_docx": "Ce document Word est illisible (fichier endommagé).",
    "encrypted": "Cette pièce est protégée par un mot de passe.",
    "telechargement_echoue": "Le téléchargement de la pièce a échoué.",
}


class _Refusal(Exception):
    """A French refusal to hand back as an error tool_result."""


def _refuse(message: str):
    raise _Refusal(message)


# ── Dossier resolution ──────────────────────────────────────────────────────


def _resolve_dossier(dossier_id: str) -> dict:
    """The dossier, or a refusal that distinguishes absent from unreadable.

    ``get_dossier`` swallows every failure into ``None``, so on a Firestore
    hiccup the honest sentence « la lecture a échoué » would come out as
    « ce dossier n'existe pas » — an assertion about the practice's records,
    made from an error, with no search run at all.
    """
    from models import dossier as dossier_model

    try:
        found = dossier_model.get_dossier_strict(dossier_id)
    except Exception:
        _refuse(
            "La lecture du dossier a échoué (erreur de base de données). "
            "Ce n'est PAS une preuve que le dossier n'existe pas — "
            "réessayez avant de conclure quoi que ce soit."
        )
    if found is None:
        _refuse(
            f"Dossier introuvable : « {dossier_id} ». "
            "L'identifiant provient de list_dossiers."
        )
    return found


def _search_basis(dossier: dict) -> tuple[list[str], list[str]]:
    """(addresses, identity terms) for a dossier — the UNION's two halves."""
    from models import partie as partie_model

    ids = mail_tools.party_ids_of(dossier)
    # get_parties_bulk fails OPEN to {}. That is right here: a missing
    # address half still leaves the identity half, and the envelope reports
    # how many addresses were used, so a degraded basis is visible rather
    # than silent.
    parties = partie_model.get_parties_bulk(ids) if ids else {}
    return mail_tools.collect_addresses(parties, ids), mail_tools.identity_terms(dossier)


# ── mail_search ─────────────────────────────────────────────────────────────


def _run_search(kql: str, received_from: str, limit: int, page_url: str = ""):
    return gm.search_messages(
        kql=kql, received_from=received_from, top=limit, page_url=page_url
    )


def _search(args: dict) -> dict:
    limit = int(args.get("limit") or 25)
    limit = max(1, min(50, limit))
    query = str(args.get("query") or "").strip()
    received_from = str(args.get("received_from") or "").strip()
    received_to = str(args.get("received_to") or "").strip()
    has_attachments = args.get("has_attachments")
    include_deleted = bool(args.get("include_deleted"))
    page_token = str(args.get("page_token") or "").strip()
    dossier_id = str(args.get("dossier_id") or "").strip()
    extra = [str(a) for a in (args.get("extra_participants") or []) if str(a).strip()]

    envelope: dict[str, Any] = {}
    searches: list[tuple[str, str]] = []   # (match_basis, kql)

    if page_token and not page_token.startswith("https://graph.microsoft.com/"):
        # It is a continuation URL, and the transport attaches the bearer
        # token to it. A value from anywhere but Graph is refused here so the
        # message names the field, and refused again at the transport so no
        # future caller can reintroduce the hole.
        _refuse(
            "page_token invalide : il doit provenir tel quel du "
            "next_page_token d'un appel précédent."
        )
    if dossier_id and page_token:
        # Two searches are merged in dossier mode, so one continuation URL
        # cannot mean anything for both — applying it to each would silently
        # re-fetch the same page twice and report it as a second page.
        _refuse(
            "page_token ne s'applique pas à une recherche par dossier : deux "
            "recherches y sont fusionnées. Restreignez plutôt avec query ou "
            "des dates."
        )

    if dossier_id:
        dossier = _resolve_dossier(dossier_id)
        addresses, terms = _search_basis(dossier)
        addresses = addresses + [a for a in extra if a.casefold() not in
                                 {x.casefold() for x in addresses}]
        envelope["dossier"] = {
            "dossier_id": dossier_id,
            "file_number": str(dossier.get("file_number") or ""),
            "addresses_used": addresses,
            "identity_terms_used": terms,
        }
        if not addresses and not terms:
            _refuse(
                "Ce dossier ne porte ni adresse de courriel ni élément "
                "d'identité (numéro, nom de partie) sur lequel chercher. "
                "Fournissez extra_participants ou query."
            )
        # The UNION. An INTERSECT here is the defect this design exists to
        # avoid: a counterparty writing from an address the file does not
        # carry is the COMMON case, and an intersect returns zero rows, which
        # reads as « ce dossier n'a aucune correspondance ».
        common = (query,) if query else ()
        if addresses:
            searches.append((
                "participants",
                gm.build_kql(
                    participants=tuple(addresses), terms=common,
                    received_from=received_from, received_to=received_to,
                    has_attachments=has_attachments,
                ),
            ))
        if terms:
            searches.append((
                "identite",
                gm.build_kql(
                    terms=tuple(terms) + common,
                    received_from=received_from, received_to=received_to,
                    has_attachments=has_attachments,
                ),
            ))
    else:
        kql = gm.build_kql(
            participants=tuple(extra),
            terms=(query,) if query else (),
            received_from=received_from, received_to=received_to,
            has_attachments=has_attachments,
        )
        searches.append(("requete" if kql else "recents", kql))

    rows: dict[str, dict] = {}
    next_token = ""
    saw_more = False
    for basis, kql in searches:
        found, next_link = _run_search(kql, received_from, limit, page_url=page_token)
        # A nextLink from EITHER branch means Graph held more back. In union
        # mode we cannot hand one out (two searches, one token would be
        # meaningless), but reporting truncated:false because we discarded it
        # would be truncation presented as completeness — on privileged
        # correspondence, and with the model reading it as « that is the whole
        # file ».
        saw_more = saw_more or bool(next_link)
        if len(searches) == 1:
            next_token = next_link
        for message in found:
            mid = str(message.get("id") or "")
            if mid and mid not in rows:
                rows[mid] = {"_basis": basis, "_msg": message}

    ordered = sorted(
        rows.values(),
        key=lambda r: str(r["_msg"].get("receivedDateTime") or ""),
        reverse=True,
    )

    deleted_id, deleted_known = gm.deleted_items_id()
    sender = (Config.GRAPH_SENDER_UPN or "").strip().casefold()

    out: list[dict] = []
    dropped_deleted = 0
    dropped_machine = 0
    folder_complete = True
    for entry in ordered:
        message = entry["_msg"]
        parent = str(message.get("parentFolderId") or "")
        if not include_deleted and deleted_known and parent and parent == deleted_id:
            dropped_deleted += 1
            continue
        # The application's own outbound mail, dropped BEST-EFFORT.
        #
        # It is best-effort and not a guarantee: reception@ is an ALIAS of
        # this very mailbox (confirmed 2026-08-28), and Exchange Online
        # rewrites an alias's P2 From to the mailbox's PRIMARY address unless
        # SendFromAliasEnabled is set — so a portal invitation may well come
        # back stamped from the juriste himself and slip past this test. The
        # obvious repair, excluding his own address, is refused: it would
        # drop half of every thread, which is the correspondence the feature
        # exists to read. The real control against the credential those
        # invitations carry is mail_tools.scrub_payload, applied to every
        # string a read returns. This test remains because it is free and it
        # helps whenever the rewrite does not happen.
        if sender and mail_tools._address_of(message.get("from")).casefold() == sender:
            dropped_machine += 1
            continue
        folder, ok = gm.folder_path(parent)
        folder_complete = folder_complete and ok
        out.append(mail_tools.shape_row(message, folder=folder, match_basis=entry["_basis"]))
        if len(out) >= limit:
            break

    envelope.update({
        "messages": out,
        "count": len(out),
        "truncated": len(ordered) > len(out) or bool(next_token) or saw_more,
        "folder_labels_complete": folder_complete,
        # Named for what it IS — a best-effort count, not a guarantee. See
        # the comment above the test that produces it.
        "app_sent_excluded_best_effort": dropped_machine,
    })
    # EXCLUSION may not fail open: claiming the Corbeille was filtered when
    # the lookup failed would present discarded mail as live correspondence.
    if include_deleted:
        envelope["deleted_items_included"] = True
    elif deleted_known:
        envelope["deleted_items_excluded"] = dropped_deleted
    else:
        envelope["deleted_items_excluded"] = None
        envelope["warning"] = (
            "Le dossier « Éléments supprimés » n'a pas pu être identifié : "
            "des courriels supprimés peuvent figurer dans ces résultats."
        )
    if next_token and len(searches) == 1:
        envelope["next_page_token"] = next_token
    elif envelope["truncated"]:
        envelope["next_page_token"] = None
        envelope.setdefault(
            "paging_note",
            "Recherche par dossier : la pagination n'est pas offerte "
            "(deux recherches sont fusionnées). Restreignez avec query ou "
            "des dates pour voir le reste.",
        )
    return envelope


# ── mail_read_thread ────────────────────────────────────────────────────────


def _thread(args: dict) -> dict:
    conversation_id = str(args.get("conversation_id") or "").strip()
    if not conversation_id:
        _refuse("conversation_id est requis (il provient de mail_search).")
    cursor = mail_tools.decode_thread_cursor(args.get("cursor"))
    if cursor is not None and cursor[0] != conversation_id:
        _refuse(
            "Ce curseur appartient à une autre conversation. Reprenez sans "
            "curseur, ou utilisez celui rendu par l'appel précédent."
        )
    messages = gm.list_conversation(conversation_id)
    rows = []
    for message in messages:
        # uniqueBody: the message MINUS the quoted history it repeats. On a
        # 60-message thread the full body would be mostly the same chain over
        # and over, and the cap would be spent on duplication.
        unique = (message.get("uniqueBody") or {}).get("content") or ""
        full = (message.get("body") or {}).get("content") or ""
        # Clipped per message, as _message already does. slice_thread ALWAYS
        # takes the first row whole (else an oversized message would advance
        # the window by nothing and the model would page forever on it), so
        # without this a single 200 KB body enters the turn uncapped.
        text, clipped = mail_tools.clip(
            (unique or full).strip(), int(Config.CHAT_MAIL_BODY_CHAR_CAP)
        )
        rows.append({
            "message_id": str(message.get("id") or ""),
            "conversation_id": conversation_id,
            "from": mail_tools._address_of(message.get("from")),
            "to": mail_tools._addresses_of(message.get("toRecipients")),
            "received": str(message.get("receivedDateTime") or ""),
            "subject": str(message.get("subject") or ""),
            "has_attachments": bool(message.get("hasAttachments")),
            "text": text,
            "message_truncated": clipped,
        })
    taken, truncated, next_cursor = mail_tools.slice_thread(
        rows, cursor=cursor, char_cap=int(Config.CHAT_MAIL_BODY_CHAR_CAP)
    )
    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "messages": taken,
        "count": len(taken),
        "total_in_thread": len(rows),
        "truncated": truncated,
    }
    if truncated:
        payload["next_cursor"] = next_cursor
        payload["note"] = (
            "Fil tronqué : suivez next_cursor jusqu'au bout avant de rédiger "
            "une réponse."
        )
    return payload


# ── mail_read_message ───────────────────────────────────────────────────────


def _attachment_row(attachment: dict) -> dict:
    kind = str(attachment.get("@odata.type") or "")
    return {
        "attachment_id": str(attachment.get("id") or ""),
        "name": str(attachment.get("name") or ""),
        "content_type": str(attachment.get("contentType") or ""),
        "size_bytes": int(attachment.get("size") or 0),
        "is_inline": bool(attachment.get("isInline")),
        "kind": (
            "fichier" if "fileAttachment" in kind
            else "courriel_imbrique" if "itemAttachment" in kind
            else "lien_infonuagique" if "referenceAttachment" in kind
            else "inconnu"
        ),
    }


def _message(args: dict) -> dict:
    message_id = str(args.get("message_id") or "").strip()
    if not message_id:
        _refuse("message_id est requis (il provient de mail_search).")
    message = gm.get_message(message_id)
    if not message.get("id"):
        _refuse(f"Courriel introuvable : « {message_id} ».")
    body = (message.get("body") or {}).get("content") or ""
    text, truncated = mail_tools.clip(body, int(Config.CHAT_MAIL_BODY_CHAR_CAP))
    folder, folder_ok = gm.folder_path(str(message.get("parentFolderId") or ""))
    attachments = (
        [_attachment_row(a) for a in gm.list_attachments(message_id)]
        if message.get("hasAttachments") else []
    )
    return {
        "message_id": message_id,
        "conversation_id": str(message.get("conversationId") or ""),
        "subject": str(message.get("subject") or ""),
        "from": mail_tools._address_of(message.get("from")),
        "to": mail_tools._addresses_of(message.get("toRecipients")),
        "cc": mail_tools._addresses_of(message.get("ccRecipients")),
        "received": str(message.get("receivedDateTime") or ""),
        "internet_message_id": str(message.get("internetMessageId") or ""),
        "folder": folder,
        "folder_label_complete": folder_ok,
        "text": text,
        "truncated": truncated,
        "attachments": attachments,
    }


# ── mail_read_attachment ────────────────────────────────────────────────────


def _nested_message_text(raw: bytes) -> str:
    """The text of a forwarded message, via the stdlib — no new dependency.

    An itemAttachment carries no contentBytes; its ``$value`` is MIME. A
    forwarded bundle from opposing counsel is the standard way earlier
    correspondence arrives, so refusing it would be a day-one gap.
    """
    parsed = email.message_from_bytes(raw, policy=policy.default)
    try:
        body = parsed.get_body(preferencelist=("plain", "html"))
    except Exception:
        body = None
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except Exception:
            text = ""
    header = " | ".join(
        f"{k}: {parsed.get(k)}" for k in ("From", "To", "Date", "Subject")
        if parsed.get(k)
    )
    return (header + "\n\n" + str(text or "")).strip()


def _unreadable(reason: str, **slots: Any) -> dict:
    template = _UNREADABLE_FR.get(reason, "Pièce illisible.")
    return {
        "readable": False,
        "reason": reason,
        "message": template.format(**slots) if slots else template,
    }


def _attachment(args: dict) -> dict:
    message_id = str(args.get("message_id") or "").strip()
    attachment_id = str(args.get("attachment_id") or "").strip()
    if not message_id or not attachment_id:
        _refuse("message_id et attachment_id sont requis (mail_read_message).")

    inventory = {a["attachment_id"]: a for a in
                 (_attachment_row(x) for x in gm.list_attachments(message_id))}
    meta = inventory.get(attachment_id)
    if meta is None:
        _refuse(
            f"Pièce jointe introuvable : « {attachment_id} ». "
            "Les identifiants proviennent de mail_read_message."
        )
    # Parsed BEFORE any byte moves: refusing a malformed range after paying
    # for a 20 MiB download would be the caller's mistake, charged to the
    # lawyer's throttle budget.
    first_page, last_page = _page_range(args)
    base = {
        "message_id": message_id,
        "attachment_id": attachment_id,
        "name": meta["name"],
        "content_type": meta["content_type"],
    }
    if meta["kind"] == "lien_infonuagique":
        return {**base, **_unreadable("lien_infonuagique")}
    cap = int(Config.CHAT_MAIL_ATTACHMENT_MAX_BYTES)
    # The byte gate on the DECLARED size, before a byte moves. graph_get_bytes
    # re-checks the real count, because declared metadata can be stale.
    if meta["size_bytes"] and meta["size_bytes"] > cap:
        return {**base, **_unreadable("piece_trop_volumineuse", size=meta["size_bytes"])}

    try:
        raw, _ctype = gm.get_attachment_bytes(message_id, attachment_id, max_bytes=cap)
    except GraphTooLarge:
        return {**base, **_unreadable("piece_trop_volumineuse", size=meta["size_bytes"])}
    except GraphError:
        return {**base, **_unreadable("telechargement_echoue")}

    if meta["kind"] == "courriel_imbrique":
        return {**base, "readable": True, "kind": "courriel_imbrique",
                "text": _nested_message_text(raw)[: int(Config.CHAT_MAIL_BODY_CHAR_CAP)]}

    mime = (meta["content_type"] or "").split(";")[0].strip().lower()
    cap_chars = int(Config.CHAT_MAIL_BODY_CHAR_CAP)
    try:
        if mime == "application/pdf" or meta["name"].lower().endswith(".pdf"):
            result = pdf_text.extract_pdf_pages(
                raw, first_page=first_page, last_page=last_page,
                char_cap=cap_chars,
            )
            if not result.readable:
                return {**base, **_unreadable(result.reason or "invalid_pdf")}
            return {
                **base, "readable": True, "pagination_unit": "page",
                "page_count": result.page_count,
                "pages": [
                    {"page": p.page, "text": p.text, "has_text": p.has_text}
                    for p in result.pages
                ],
                # The honesty field. has_text False means « no text layer »,
                # never « blank on paper », and nothing here is OCR'd.
                "pages_without_text": result.pages_without_text,
                "truncated": result.truncated,
                "next_page": result.next_page,
            }
        if mime == _DOCX_MIME or meta["name"].lower().endswith(".docx"):
            paragraphs = pdf_text.extract_docx_text(raw)
            text, truncated = mail_tools.clip("\n\n".join(paragraphs), cap_chars)
            return {**base, "readable": True, "text": text, "truncated": truncated}
    except pdf_text.DocumentTextError as exc:
        return {**base, **_unreadable(str(getattr(exc, "reason", "") or "invalid_pdf"))}

    return {**base, **_unreadable("type_non_extractible", type=mime or "inconnu")}


def _page_range(args: dict) -> tuple[int, Optional[int]]:
    raw = str(args.get("page_range") or "").strip()
    if not raw:
        return 1, None
    parts = raw.split("-", 1)
    try:
        first = int(parts[0])
        last = int(parts[1]) if len(parts) == 2 else None
    except ValueError:
        _refuse(
            f"page_range invalide : « {raw} ». Formats admis : « 4 » "
            "(à partir de la page 4) ou « 2-6 » (inclusif)."
        )
    if first < 1 or (last is not None and last < first):
        _refuse(f"page_range invalide : « {raw} ».")
    return first, last


# ── mail_draft ──────────────────────────────────────────────────────────────


def _find_marked_draft(key: str) -> Optional[dict]:
    """The draft already staged under *key*, or None — FAIL OPEN.

    A failed lookup means we create, and the worst case is one extra inert
    draft the lawyer deletes in a gesture. Failing closed would mean not
    drafting at all, which is silent non-delivery of work he asked for.
    """
    try:
        for draft in gm.list_marked_drafts():
            if gm.marker_of(draft) == key:
                return draft
    except GraphError:
        return None
    return None


def _draft_payload(draft: dict, *, mode: str, resumed: bool) -> dict:
    return {
        "staged": True,
        "sent": False,
        "mode": mode,
        "outcome": "resumed" if resumed else "created",
        "draft_id": str(draft.get("id") or ""),
        "web_link": str(draft.get("webLink") or ""),
        "note": (
            "Brouillon déposé dans Outlook. Rien n'a été envoyé : l'avocat "
            "l'ouvre, le corrige et l'expédie lui-même."
        ),
    }


def _draft(args: dict, ctx: dict) -> dict:
    mode = str(args.get("mode") or "").strip()
    body = str(args.get("body") or "").strip()
    if not body:
        _refuse("body est requis : un brouillon vide n'aide personne.")
    message_id = str(args.get("message_id") or "").strip()
    recipients = tuple(
        str(a).strip() for a in (args.get("to") or []) if str(a).strip()
    )
    if mode != "new" and not message_id:
        _refuse(
            f"message_id est requis pour mode « {mode} » : une réponse "
            "s'accroche à un courriel existant."
        )
    if mode in ("new", "forward") and not recipients:
        _refuse(
            f"to est requis pour mode « {mode} » — sans destinataire, le "
            "brouillon s'ouvre adressé à personne."
        )
    if len(recipients) > int(Config.CHAT_MAIL_MAX_ADDRESSES):
        _refuse(
            f"Trop de destinataires ({len(recipients)}). Maximum "
            f"{Config.CHAT_MAIL_MAX_ADDRESSES}."
        )

    key = mail_tools.draft_key(
        str(ctx.get("idempotency_seed") or ""),
        mail_tools.DRAFT,
        str(ctx.get("tool_use_id") or ""),
    )
    existing = _find_marked_draft(key)
    if existing is not None:
        # A Cloud Tasks redelivery of the same step. Resume rather than
        # stage a second copy of the same letter.
        return _draft_payload(existing, mode=mode, resumed=True)

    if mode == "new":
        draft = gm.create_new_draft(
            to=recipients,
            subject=str(args.get("subject") or "").strip(),
            body_text=body,
            marker=key,
        )
    else:
        draft = gm.create_anchored_draft(
            message_id, mode, to=recipients if mode == "forward" else ()
        )
        draft_id = str(draft.get("id") or "")
        if not draft_id:
            _refuse("Outlook n'a pas rendu d'identifiant de brouillon.")
        # Body and marker in ONE request: a crash between two would leave an
        # unmarked draft the duplicate check can never find again.
        gm.set_draft_body(draft_id, body, marker=key)
    return _draft_payload(draft, mode=mode, resumed=False)


# ── mail_file_to_dossier ────────────────────────────────────────────────────


def _message_key(internet_message_id: str) -> str:
    """The Message-ID with its angle brackets removed.

    NOT cosmetic. ``security.sanitize`` strips every ``<...>`` run (its
    ``_TAG_RE``), and an RFC-5322 Message-ID is exactly that shape — so the
    raw value reaches Firestore as the EMPTY STRING through
    ``models.document._sanitize_data``. Storing it bracket-free makes the
    field independent of what sanitize happens to remove, on the one path
    whose entire justification is traceability. Both the write and the
    duplicate comparison normalize, so they cannot drift apart.
    """
    return str(internet_message_id or "").strip().strip("<>")


def _already_filed(dossier_id: str, internet_message_id: str) -> list[str]:
    """Documents in this dossier already filed from the same message.

    Fails OPEN (list_documents does), so a read failure files a duplicate
    rather than refusing. A duplicate is visible in the documents list; a
    refusal on a hiccup would look like the message was already handled.
    """
    key = _message_key(internet_message_id)
    if not key:
        return []
    from models import document as document_model

    try:
        rows = document_model.list_documents(dossier_id=dossier_id)
    except Exception:
        return []
    return [
        str(row.get("display_name") or "")
        for row in rows
        if str(row.get("courriel_message_id") or "") == key
    ]


def _file_one(
    *, dossier, folder_id, owner_uid, data: bytes, filename: str,
    display_name: str, category: str, message: dict,
) -> tuple[Optional[dict], list[str]]:
    from models import document as document_model

    metadata = {
        "category": category,
        "display_name": display_name,
        # Provenance in DEDICATED fields, never in the description — that is
        # the ONE free-text field the lawyer's edit form offers, and the
        # portal lot learned on 2026-08-27 that squatting it makes him choose
        # between describing a document and keeping its traceability.
        "courriel_message_id": _message_key(message.get("internet_message_id")),
        "courriel_expediteur": str(message.get("from") or ""),
        "courriel_objet": str(message.get("subject") or ""),
        "courriel_recu_le": str(message.get("received") or ""),
        "tags": ["courriel"],
        "folder_id": folder_id,
    }
    return document_model.upload_document(
        dossier["id"],
        str(dossier.get("file_number") or ""),
        io.BytesIO(data),
        filename,
        len(data),
        metadata,
        owner_uid,
    )


def _file_to_dossier(args: dict, ctx: dict) -> dict:
    from models.folder import get_or_create_folder

    message_id = str(args.get("message_id") or "").strip()
    if not message_id:
        _refuse("message_id est requis (il provient de mail_search).")
    dossier_id = (
        str(args.get("dossier_id") or "").strip()
        or str(ctx.get("conversation_dossier_id") or "").strip()
    )
    if not dossier_id:
        _refuse(
            "dossier_id est requis : cette conversation n'est rattachée à "
            "aucun dossier, il faut donc nommer celui où verser."
        )
    owner_uid = str(ctx.get("owner_uid") or "").strip()
    if not owner_uid:
        _refuse("Le versement est indisponible : propriétaire introuvable.")
    dossier = _resolve_dossier(dossier_id)

    message = _message({"message_id": message_id})
    # Validated HERE, against the model's own vocabulary rather than a copied
    # literal: upload_document would refuse with a bare « Catégorie invalide »
    # that names neither the field nor what is allowed, and the house rule is
    # that a refusal naming the field beats a late model error.
    from models.document import VALID_CATEGORIES

    category = str(args.get("category") or "correspondance").strip()
    if category not in VALID_CATEGORIES:
        _refuse(
            f"Catégorie inconnue : « {category} ». Valeurs admises : "
            + ", ".join(VALID_CATEGORIES)
            + "."
        )
    wanted = [str(a) for a in (args.get("attachment_ids") or []) if str(a).strip()]
    if len(wanted) > int(Config.CHAT_MAIL_MAX_ATTACHMENTS_PER_CALL):
        _refuse(
            f"Trop de pièces en un appel ({len(wanted)}). Maximum "
            f"{Config.CHAT_MAIL_MAX_ATTACHMENTS_PER_CALL} ; rappelez l'outil."
        )
    include_message = args.get("include_message")
    include_message = True if include_message is None else bool(include_message)

    duplicates = _already_filed(dossier_id, message.get("internet_message_id", ""))
    folder = get_or_create_folder(dossier_id, mail_tools.MAIL_FOLDER_NAME)
    folder_id = folder["id"] if folder else None

    filed: list[dict] = []
    refused: list[dict] = []

    if include_message:
        mime = b""
        try:
            mime = gm.get_message_mime(message_id)
        except GraphTooLarge:
            refused.append({"what": "courriel", "reason": "piece_trop_volumineuse"})
        if mime:
            display = mail_tools.mail_document_name(
                message.get("received", ""), message.get("subject", "")
            )
            doc, errors = _file_one(
                dossier=dossier, folder_id=folder_id, owner_uid=owner_uid,
                data=mime,
                filename=mail_tools.safe_filename(
                    message.get("subject", ""), ".eml"
                ),
                display_name=display, category=category, message=message,
            )
            if doc:
                filed.append({"document_id": doc["id"], "name": display,
                              "kind": "courriel"})
            else:
                refused.append({"what": "courriel", "reason": "; ".join(errors)})

    inventory = {a["attachment_id"]: a for a in message.get("attachments", [])}
    for attachment_id in wanted:
        meta = inventory.get(attachment_id)
        if meta is None:
            refused.append({"what": attachment_id, "reason": "introuvable"})
            continue
        if meta["kind"] == "lien_infonuagique":
            refused.append({"what": meta["name"], "reason": "lien_infonuagique"})
            continue
        try:
            data, _ctype = gm.get_attachment_bytes(message_id, attachment_id)
        except GraphTooLarge:
            refused.append({"what": meta["name"],
                            "reason": "piece_trop_volumineuse"})
            continue
        except GraphError:
            refused.append({"what": meta["name"],
                            "reason": "telechargement_echoue"})
            continue
        # A forwarded message arrives as MIME and is filed as a .eml — the
        # type is already in the documents vocabulary.
        name = meta["name"] or "piece"
        if meta["kind"] == "courriel_imbrique" and not name.lower().endswith(".eml"):
            name = f"{name}.eml"
        if "." not in name:
            # Never GUESS an extension. upload_document checks that the
            # sniffed type agrees with the extension, so a guessed « .pdf »
            # on a Word file is refused with a message about content, which
            # sends the reader looking for a corrupt file that is fine.
            refused.append({"what": name, "reason": "extension_absente"})
            continue
        extension = "." + name.rsplit(".", 1)[-1]
        doc, errors = _file_one(
            dossier=dossier, folder_id=folder_id, owner_uid=owner_uid,
            data=data,
            filename=mail_tools.safe_filename(
                name.rsplit(".", 1)[0], extension, fallback="piece"
            ),
            display_name=name, category=category, message=message,
        )
        if doc:
            filed.append({"document_id": doc["id"], "name": name,
                          "kind": "piece_jointe"})
        else:
            refused.append({"what": name, "reason": "; ".join(errors)})

    payload: dict[str, Any] = {
        "dossier_id": dossier_id,
        "file_number": str(dossier.get("file_number") or ""),
        "folder": mail_tools.MAIL_FOLDER_NAME,
        "filed": filed,
        "filed_count": len(filed),
        "refused": refused,
    }
    if duplicates:
        # Not a refusal — the lawyer may want a second copy in another
        # dossier, and there is no delete tool to undo a wrong guess.
        payload["already_filed_here"] = duplicates
        payload["note"] = (
            "Ce courriel figurait DÉJÀ dans ce dossier ; une nouvelle copie "
            "vient d'être versée. Aucun outil ne peut la supprimer."
        )
    return payload


# ── Dispatch ────────────────────────────────────────────────────────────────

_READS = {
    mail_tools.SEARCH: _search,
    mail_tools.READ_THREAD: _thread,
    mail_tools.READ_MESSAGE: _message,
    mail_tools.READ_ATTACHMENT: _attachment,
}
# The writes take the turn context (owner uid, dossier, idempotency seed);
# the reads do not, and handing it to them would invite a read to start
# depending on the conversation rather than on its arguments.
_WRITES = {
    mail_tools.DRAFT: _draft,
    mail_tools.FILE_TO_DOSSIER: _file_to_dossier,
}


def run(name: str, arguments: dict, *, context: Optional[dict] = None) -> tuple[Any, bool]:
    """Execute one mail tool. Returns (payload, is_error) — never raises."""
    handler = _READS.get(name)
    writer = _WRITES.get(name)
    if handler is None and writer is None:
        return f"Unknown mail tool: {name}.", True
    try:
        if writer is not None:
            # Scrubbed too. There were two exits and only one control, which
            # is exactly the drift the « one seam » wording exists to prevent
            # — and a write payload echoes names and refusal reasons drawn
            # from the message it acted on.
            return mail_tools.scrub_payload(writer(arguments, context or {})), False
        # Scrubbed at ONE seam. A control applied per call site is a control
        # eventually forgotten at a new call site, and what would be
        # forgotten here is a live single-use sign-in credential sitting in
        # the mailbox's own Sent Items — bound for an append-only registre
        # with no delete path.
        return mail_tools.scrub_payload(handler(arguments)), False
    except _Refusal as exc:
        return str(exc), True
    except gm.MailBudgetExhausted as exc:
        return str(exc), True
    except gm.MailRefused as exc:
        return str(exc), True
    except GraphNotConfigured:
        return (
            "La messagerie n'est pas configurée sur ce service.", True
        )
    except GraphError:
        # Status only, never a Graph body — it can echo tenant identifiers.
        return (
            "L'accès à la boîte de courriels a échoué. Réessayez ; si cela "
            "persiste, signalez-le à l'avocat.", True
        )
    except Exception:
        # The docstring promises this function never raises, and the promise
        # has to be true: execute_tool does not wrap the mail branch, so an
        # escaping exception would cross turn_engine's span — which calls
        # record_exception and would ship a fragment of privileged mail to
        # Cloud Trace — and then fail the whole turn.
        log_unexpected("chat mail tool failed", tool=name)
        return (
            "L'outil de messagerie a échoué pour une raison inattendue. "
            "L'erreur est journalisée ; réessayez ou reformulez.", True
        )
