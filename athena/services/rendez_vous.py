"""Bookings rendez-vous — Réception today, the MCP connector from lot 1b.

A « Bookings with me » reservation is imported by the 10-minute sync
(``routes/taches_bookings.py``) as a hearing gated behind
``confirmation == "à_confirmer"`` — invisible to DAV, the connector and the
dashboard until the lawyer decides. What lives HERE is that decision, so
that Réception and the connector (lot 1b, ``decide_rendez_vous``) cannot
make it two ways:

* **reading** what awaits a decision through a STRICT reader
  (``models.hearing.list_bookings_strict``) — never the fail-open
  ``list_hearings(include_unconfirmed=True)``, which answered a Firestore
  outage with « rien en attente ». A failed read raises
  :class:`LectureImpossible`; the caller says so;
* **confirming**: the event enters the calendar and DAV (the CTag of its
  collection is bumped). The linked contact is matched SERVER-SIDE on the
  requester's exact address — a caller never supplies a ``partie_id``.
  Confirming an import the CLIENT cancelled is refused;
* **refusing**: the Outlook meeting is cancelled through Graph — which
  notifies the client (the Bookings contract): an OUTBOUND effect. So the
  caller's version is checked BEFORE the Graph call (a page older than the
  sync's last update is refused with nothing done), the cancellation text
  is fixed (:data:`REFUS_MOTIF` — never a caller's prose), and the local
  write that follows the call is UNCONDITIONAL: once the client has been
  notified, the refusal is the truth, and no concurrency check may turn it
  into a retryable error — a retry would call Graph again on a cancelled
  event and tell the lawyer to « cancel manually » a meeting already
  cancelled. A local failure after a successful cancellation is therefore
  a SUCCESS carrying a warning (``graph_cancelled`` True), never an error.
  A refusal bumps no CTag: a pending import was never in DAV.

The intake-invitation email (L3's trigger (a)) is NOT here: it stays in
``routes/reception.rdv_confirmer``, which alone sends it.

Provenance (``updated_via``) comes from the writer's context
(``models.provenance``) — the request's blueprint or the connector's
``writing_via`` — never from an argument (Architecture Rule 5's lot-0a
corollary). Every function returns ``(hearing, errors, report)``: errors
non-empty means NOTHING was WRITTEN, and — on every path but one — that
Outlook was not called either. The exception is :func:`refuser` when the
Graph cancellation itself FAILED and the local write then failed too:
``report["graph_attempted"]`` is True there. A retry is still the right
answer (the cancellation did not take, as far as anything here can know),
which is why that path is an error and not a warned success. The report
says what did happen, in flags a banner (or a tool payload) can state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from config import Config
from dav.sync import bump_ctag, collection_for, record_tombstone, remove_tombstone
from models import concurrency, provenance
from models import hearing as hearing_model
from models import partie as partie_model
from utils import graph_calendrier
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event, log_unexpected

# The text Graph carries into the cancellation the CLIENT receives. Fixed:
# the connector's first message to a third party is a sentence the lawyer
# has read, never one a model composed.
REFUS_MOTIF = "Rendez-vous refusé par le juriste."

# What a caller with no banner of its own says on LectureImpossible (lot
# 1b's connector); Réception's template prints the same sentence.
LECTURE_IMPOSSIBLE = (
    "Lecture des rendez-vous impossible — réessayez dans un moment."
)
ANNULE_PAR_LE_CLIENT = (
    "Le client a annulé ce rendez-vous : il ne peut plus être confirmé. "
    "Retirez-le de la réception."
)
DEJA_REFUSE = "Ce rendez-vous a déjà été refusé."
DEJA_CONFIRME_NE_SE_REFUSE_PLUS = (
    "Ce rendez-vous est déjà confirmé : il ne peut plus être refusé d'ici. "
    "Rien n'a été fait."
)
SANS_COURRIEL = (
    "Ce rendez-vous ne porte aucun courriel : aucun contact ne peut y être "
    "lié. Rien n'a été confirmé."
)
AUCUN_CONTACT = (
    "Aucun contact ne porte le courriel de ce rendez-vous — rechargez la "
    "page. Rien n'a été confirmé."
)
CONTACTS_ILLISIBLES = (
    "Impossible de vérifier quel contact porte ce courriel. Rien n'a été "
    "confirmé — réessayez dans un moment."
)
ANNULATION_OUTLOOK_ECHOUEE = (
    "Rendez-vous refusé, mais la réunion n'a PAS pu être annulée côté "
    "Outlook — annulez-la manuellement pour prévenir le client."
)
# A LIVE import (« à_confirmer ») refused while Outlook cannot be reached at
# all — the Graph link is not configured, or the import carries no event id.
# Nothing was attempted, so nothing failed; but the client's meeting is still
# booked and nobody told him. « Rendez-vous refusé. » alone would read as the
# whole job done — the old route's silence, which lot 1b's connector would
# have relayed as a clean refusal.
ANNULATION_OUTLOOK_NON_TENTEE = (
    "Rendez-vous refusé dans Athéna, mais la réunion Outlook n'a PAS été "
    "annulée : la liaison avec Outlook n'est pas configurée, ou ce "
    "rendez-vous ne porte aucune référence d'événement — annulez-la "
    "manuellement pour prévenir le client."
)
ANNULATION_OUTLOOK_INCERTAINE = (
    "Rendez-vous refusé, mais l'annulation de la réunion côté Outlook a "
    "échoué de façon inattendue — vérifiez-la dans Outlook et annulez-la au "
    "besoin pour prévenir le client."
)
REFUS_NON_INSCRIT = (
    "La réunion Outlook a été annulée et le client notifié, mais le refus "
    "n'a pas pu être inscrit dans Athéna. Ne cliquez pas « Refuser » de "
    "nouveau : la synchronisation Bookings (10 min au plus) le marquera "
    "« Annulé par le client » ; retirez-le alors."
)
SYNCHRO_NON_AVERTIE = (
    "Les appareils synchronisés n'ont pas pu en être avertis — ils le "
    "verront à la prochaine modification du calendrier."
)


class LectureImpossible(Exception):
    """A strict read failed: what awaits a decision is UNKNOWN.

    Never to be rendered as « rien en attente » — the whole point of the
    strict readers.
    """


# ── Lecture ─────────────────────────────────────────────────────────────


def index_parties_par_courriel() -> dict[str, dict]:
    """``{address: partie}`` — strict (:class:`LectureImpossible`)."""
    try:
        return partie_model.index_by_email_strict()
    except Exception as exc:
        log_unexpected("rendez-vous: contacts index unreadable")
        raise LectureImpossible() from exc


def partie_du_courriel(hearing: dict, index: dict[str, dict]) -> Optional[dict]:
    """The contact whose address is the requester's, exactly — or None."""
    courriel = str(hearing.get("client_email") or "").strip().lower()
    return index.get(courriel) if courriel else None


def lier_parties(hearings: list[dict], index: dict[str, dict]) -> None:
    """Attach ``_partie_id`` / ``_partie_nom`` to each rendez-vous — the
    contact :func:`confirmer` would link. Precomputed so a template stays
    logic-free."""
    for h in hearings:
        p = partie_du_courriel(h, index)
        h["_partie_id"] = p.get("id", "") if p else ""
        h["_partie_nom"] = partie_model.display_name(p) if p else ""


def _bookings(*, include_confirmed: bool) -> list[dict]:
    try:
        return hearing_model.list_bookings_strict(
            include_confirmed=include_confirmed
        )
    except Exception as exc:
        log_unexpected("rendez-vous: bookings read failed")
        raise LectureImpossible() from exc


def lister_en_attente() -> list[dict]:
    """The imports awaiting a decision — ``à_confirmer`` and
    ``annulée_client`` — chronological, each carrying the contact
    :func:`confirmer` would link. Raises :class:`LectureImpossible`."""
    rows = _bookings(include_confirmed=False)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda h: h.get("start_datetime") or floor)
    if rows:
        lier_parties(rows, index_parties_par_courriel())
    return rows


def lister_divergences() -> list[dict]:
    """Imports carrying an unseen ``bookings_divergence`` — a CONFIRMED
    event the client moved or cancelled on the Bookings side (the sync
    never overwrites a confirmed one). Raises :class:`LectureImpossible`."""
    return [
        h for h in _bookings(include_confirmed=True)
        if (h.get("bookings_divergence") or {}).get("motif")
        and not (h.get("bookings_divergence") or {}).get("vu")
    ]


# ── Décisions ───────────────────────────────────────────────────────────


def _read(hid: str) -> tuple[Optional[dict], list[str]]:
    """The stored import, read strictly. ``get_hearing`` is fail-open: a
    blip would have answered « introuvable » to a decision about a live
    reservation."""
    try:
        hearing = hearing_model.get_hearing_strict(hid)
    except Exception:
        log_unexpected("rendez-vous: hearing read failed")
        return None, [hearing_model.CONFIRMATION_READ_ERROR]
    if not hearing or hearing.get("source") != "bookings":
        return None, ["Rendez-vous introuvable."]
    return hearing, []


def _guard(hearing: dict, expected_etag: Optional[str]) -> str:
    """The version a decision commits against: the caller's when it gave
    one, else the one this service read (plan rule 3 — never a blind
    write when nothing external has happened yet)."""
    if expected_etag is not None:
        return expected_etag
    return concurrency.etag_of(hearing)


def _enter_dav(hearing: dict, hid: str) -> bool:
    """The confirmed event enters its DAV collection (``""`` → « Général »
    for a Bookings import): drop any stale tombstone, bump. After the
    commit, so never raised — a raise would report a committed
    confirmation as a failure."""
    try:
        name = collection_for(hearing.get("dossier_id"))
        remove_tombstone(name, hid)
        bump_ctag(name)
        return True
    except Exception:
        log_unexpected("rendez-vous: DAV bump failed after a confirmation",
                       hearing_id=hid)
        return False


def _leave_dav(hearing: dict, hid: str) -> bool:
    """A CONFIRMED event leaving the live set needs a tombstone — a bump
    alone leaves it on the phone (RFC 6578)."""
    try:
        name = collection_for(hearing.get("dossier_id"))
        record_tombstone(name, hid)
        bump_ctag(name)
        return True
    except Exception:
        log_unexpected("rendez-vous: DAV tombstone failed after a refusal",
                       hearing_id=hid)
        return False


def confirmer(
    hid: str,
    *,
    lier_partie: bool,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Confirm a pending import: it enters the calendar and DAV.

    ``lier_partie``: link the contact whose address is the requester's,
    EXACTLY — matched here, on a strict read. Asked for but impossible (no
    address, no such contact, contacts unreadable) → refused, nothing
    written: linking the wrong contact, or silently none, is worse than
    asking the lawyer to reload. ``expected_etag``: the version the caller
    was shown (the sync may since have moved the slot — confirming would
    then confirm a time nobody saw).

    Report: ``changed`` (False when already confirmed — a replay writes
    nothing), ``partie_liee``, ``partie_id``, ``dav_synced``, ``warning``.
    """
    report = {"changed": False, "partie_liee": False, "partie_id": "",
              "dav_synced": False, "warning": ""}
    hearing, errors = _read(hid)
    if errors:
        return None, errors, report
    etat = hearing.get("confirmation") or ""
    if etat == "":
        # Already confirmed: answered BEFORE the version check, whatever
        # etag is given — there is no decision to lose, and a double click
        # (or a replay) must read « déjà confirmé », not « a changé ».
        report["dav_synced"] = True
        return hearing, [], report
    if not concurrency.matches(hearing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], report
    if etat == "annulée_client":
        return None, [ANNULE_PAR_LE_CLIENT], report
    if etat == "refusée":
        return None, [DEJA_REFUSE], report

    partie_id = None
    if lier_partie:
        if not str(hearing.get("client_email") or "").strip():
            return None, [SANS_COURRIEL], report
        try:
            partie = partie_du_courriel(hearing, index_parties_par_courriel())
        except LectureImpossible:
            return None, [CONTACTS_ILLISIBLES], report
        if not partie:
            return None, [AUCUN_CONTACT], report
        partie_id = partie["id"]

    # Without a caller version, compare-and-set against the one read HERE:
    # the transition rules above were judged on it, and the sync may flip
    # the import to annulée_client in between.
    written, errors, _previous = hearing_model.set_bookings_confirmation(
        hid, "", partie_id=partie_id, expected_etag=_guard(hearing, expected_etag),
    )
    if errors:
        return None, errors, report
    report.update(changed=True, partie_liee=partie_id is not None,
                  partie_id=partie_id or "")
    report["dav_synced"] = _enter_dav(written, hid)
    if not report["dav_synced"]:
        report["warning"] = SYNCHRO_NON_AVERTIE
    log_bookings_event("reception_rdv_confirme", hearing_id=hid,
                       partie_liee=report["partie_liee"],
                       via=provenance.current_via())
    return written, [], report


def refuser(
    hid: str,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Refuse a pending import (or remove one the client cancelled).

    Order, and why:

    1. read strictly; an import already refused is answered at once (no
       write, never a second Graph call); otherwise the caller's version is
       compared FIRST — a stale page is refused before anything leaves the
       building;
    2. for a still-active ``à_confirmer`` import with a Graph event, the
       Outlook meeting is cancelled with :data:`REFUS_MOTIF` (the client is
       notified). An ``annulée_client`` import is already cancelled on the
       client's side: Graph is not called (it would 404). A still-active
       import Outlook CANNOT be asked about (Graph unconfigured, no event
       id) is refused locally with :data:`ANNULATION_OUTLOOK_NON_TENTEE` —
       its client is still booked, and a bare « refusé » would hide that;
    3. the refusal is written. After a Graph call it is UNCONDITIONAL (no
       etag): the external effect happened, and the refusal is the truth
       whatever landed meanwhile — if another tab confirmed the import in
       between, the confirmed event is tombstoned out of DAV. Without a
       Graph call the write is guarded — by ``expected_etag``, or by the
       version read in step 1.

    A local failure after a SUCCESSFUL cancellation is retried once, then
    reported as success with :data:`REFUS_NON_INSCRIT` — never an error a
    caller would retry into a second Graph call.

    Report: ``changed``, ``graph_attempted``, ``graph_cancelled``,
    ``local_written``, ``warning``.
    """
    report = {"changed": False, "graph_attempted": False,
              "graph_cancelled": False, "local_written": False,
              "warning": ""}
    hearing, errors = _read(hid)
    if errors:
        return None, errors, report
    etat = hearing.get("confirmation") or ""
    if etat == "refusée":
        # Already refused: answered before the version check (see
        # confirmer) — and, above all, never a second Graph call.
        return hearing, [], report
    if not concurrency.matches(hearing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], report
    if etat == "":
        return None, [DEJA_CONFIRME_NE_SE_REFUSE_PLUS], report

    gid = hearing.get("graph_event_id")
    # Why Outlook was NOT asked about a live import that needed it — None
    # when it was asked, or did not need to be (annulée_client).
    non_tente: Optional[str] = None
    if etat == "à_confirmer" and not (gid and Config.bookings_configured()):
        non_tente = "not_configured" if gid else "sans_evenement"
        report["warning"] = ANNULATION_OUTLOOK_NON_TENTEE
    elif etat == "à_confirmer":
        report["graph_attempted"] = True
        try:
            graph_calendrier.annuler_reservation(gid, REFUS_MOTIF)
            report["graph_cancelled"] = True
        except (GraphError, GraphNotConfigured):
            log_unexpected("rendez-vous: graph cancel failed", hearing_id=hid)
            report["warning"] = ANNULATION_OUTLOOK_ECHOUEE
        except Exception:
            log_unexpected("rendez-vous: graph cancel raised unexpectedly",
                           hearing_id=hid)
            report["warning"] = ANNULATION_OUTLOOK_INCERTAINE

    guard = None if report["graph_attempted"] else _guard(hearing, expected_etag)
    written, errors, previous = hearing_model.set_bookings_confirmation(
        hid, "refusée", expected_etag=guard,
    )
    if errors and report["graph_cancelled"]:
        written, errors, previous = hearing_model.set_bookings_confirmation(
            hid, "refusée",
        )
    if errors:
        if report["graph_cancelled"]:
            report["warning"] = REFUS_NON_INSCRIT
            log_bookings_event(
                "reception_rdv_refuse", "failure", hearing_id=hid,
                graph_annule=True, reason="ecriture_locale",
                via=provenance.current_via(),
            )
            return None, [], report
        return None, errors, report

    report.update(changed=True, local_written=True)
    if previous == "":
        # Confirmed by another tab between our read and the cancellation:
        # it had entered DAV, and must leave it.
        _leave_dav(written, hid)
    failed = report["graph_attempted"] and not report["graph_cancelled"]
    reason = "graph_error" if failed else non_tente
    log_bookings_event(
        "reception_rdv_refuse", "refused" if reason else "success",
        hearing_id=hid, graph_annule=report["graph_cancelled"],
        reason=reason,
        via=provenance.current_via(),
    )
    return written, [], report
