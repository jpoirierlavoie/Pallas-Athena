"""CardDAV server — RFC 6352 endpoints for contacts (parties).

Endpoints
---------
/dav/addressbook/                   Address-book collection
/dav/addressbook/<partie_id>.vcf    Individual contact resource
"""

import logging
import xml.etree.ElementTree as ET

from flask import Blueprint, Response, request

from dav.dav_auth import dav_auth_required
from dav.sync import (
    bump_ctag,
    get_ctag,
    get_sync_token,
    get_tombstones,
    record_tombstone,
    remove_tombstone,
)
from dav.xml_utils import (
    add_propstat,
    add_response,
    add_status_response,
    carddav_tag,
    cs_tag,
    dav_tag,
    make_multistatus,
    parse_propfind_body,
    parse_report_body,
    propfind_requests_prop,
    serialize_multistatus,
)
from models.audit_event import record_deletion
from models.dav_ids import DAV_ID_INVALID, DAV_ID_TAKEN, valid_resource_id
from models.partie import (
    MANDATAIRE_CHECK_UNAVAILABLE,
    PARTIE_DELETE_CHECK_UNAVAILABLE,
    PARTIE_DELETE_FAILED,
    PARTIE_NOT_FOUND,
    create_partie,
    delete_partie,
    display_name,
    get_partie,
    list_parties,
    partie_to_vcard,
    update_partie,
    vcard_to_partie,
)
from utils.logging_setup import log_dav_operation, sanitize_log_value
from utils.tracing_setup import add_attributes, firestore_span

logger = logging.getLogger(__name__)

carddav_bp = Blueprint("carddav", __name__)

COLLECTION_NAME = "parties"

# What DavX5 shows in its collection list. Short on purpose — the label
# is truncated mid-word there and in the Android contacts account.
ADDRESSBOOK_DISPLAY_NAME = "Clients et parties impliqués"
COLLECTION_PATH = "/dav/addressbook/"

_PAYLOAD_TOO_LARGE = "Corps de requête trop volumineux."


# ── OPTIONS ────────────────────────────────────────────────────────────────

@carddav_bp.route("/dav/addressbook/", methods=["OPTIONS"])
@carddav_bp.route("/dav/addressbook/<partie_id>.vcf", methods=["OPTIONS"])
@dav_auth_required
def options(partie_id: str = None) -> Response:
    resp = Response("", status=200)
    resp.headers["Allow"] = "OPTIONS, GET, PUT, DELETE, PROPFIND, REPORT"
    resp.headers["DAV"] = "1, 2, 3, addressbook"
    return resp


# ── PROPFIND ───────────────────────────────────────────────────────────────

@carddav_bp.route("/dav/addressbook/", methods=["PROPFIND"])
@dav_auth_required
def propfind_collection() -> Response:
    depth = request.headers.get("Depth", "0")
    add_attributes(
        **{
            "dav.collection_type": "addressbook",
            "dav.operation": "propfind",
            "dav.depth": depth,
        }
    )
    try:
        body = parse_propfind_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)
    multistatus = make_multistatus()

    # Collection response (always included)
    _add_collection_response(multistatus, body)

    # Depth:1 — include individual resources
    if depth == "1":
        with firestore_span("query", COLLECTION_NAME):
            parties = list_parties()
        for partie in parties:
            _add_resource_response(multistatus, partie, body)
        add_attributes(**{"dav.object_count": len(parties)})

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


@carddav_bp.route("/dav/addressbook/<partie_id>.vcf", methods=["PROPFIND"])
@dav_auth_required
def propfind_resource(partie_id: str) -> Response:
    partie = get_partie(partie_id)
    if not partie:
        return Response("Not Found", status=404)

    try:
        body = parse_propfind_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)
    multistatus = make_multistatus()
    _add_resource_response(multistatus, partie, body)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _add_collection_response(
    multistatus: ET.Element, body: ET.Element | None
) -> None:
    """Add the collection's own <D:response> to the multistatus."""
    resp = add_response(multistatus, COLLECTION_PATH)
    prop = add_propstat(resp)

    # resourcetype
    if propfind_requests_prop(body, dav_tag("resourcetype")):
        rt = ET.SubElement(prop, dav_tag("resourcetype"))
        ET.SubElement(rt, dav_tag("collection"))
        ET.SubElement(rt, carddav_tag("addressbook"))

    # displayname
    if propfind_requests_prop(body, dav_tag("displayname")):
        ET.SubElement(prop, dav_tag("displayname")).text = (
            "Pallas Athena \u2014 Clients"
        )

    # getctag
    if propfind_requests_prop(body, cs_tag("getctag")):
        ET.SubElement(prop, cs_tag("getctag")).text = get_ctag(COLLECTION_NAME)

    # sync-token
    if propfind_requests_prop(body, dav_tag("sync-token")):
        ET.SubElement(prop, dav_tag("sync-token")).text = (
            f"data:,{get_sync_token(COLLECTION_NAME)}"
        )

    # supported-report-set
    if propfind_requests_prop(body, dav_tag("supported-report-set")):
        srs = ET.SubElement(prop, dav_tag("supported-report-set"))
        sr = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr, dav_tag("report")).append(
            ET.Element(dav_tag("sync-collection"))
        )
        sr2 = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr2, dav_tag("report")).append(
            ET.Element(carddav_tag("addressbook-query"))
        )
        sr3 = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr3, dav_tag("report")).append(
            ET.Element(carddav_tag("addressbook-multiget"))
        )


def _add_resource_response(
    multistatus: ET.Element,
    partie: dict,
    body: ET.Element | None,
) -> None:
    """Add a single contact's <D:response> to the multistatus."""
    href = f"/dav/addressbook/{partie['id']}.vcf"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    # getetag
    if propfind_requests_prop(body, dav_tag("getetag")):
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{partie.get("etag", "")}"'

    # getcontenttype
    if propfind_requests_prop(body, dav_tag("getcontenttype")):
        ET.SubElement(prop, dav_tag("getcontenttype")).text = (
            "text/vcard; charset=utf-8"
        )

    # resourcetype (empty for non-collection resources)
    if propfind_requests_prop(body, dav_tag("resourcetype")):
        ET.SubElement(prop, dav_tag("resourcetype"))

    # address-data (only when explicitly requested)
    if body is not None and propfind_requests_prop(
        body, carddav_tag("address-data")
    ):
        ET.SubElement(prop, carddav_tag("address-data")).text = (
            partie_to_vcard(partie)
        )


# ── REPORT ─────────────────────────────────────────────────────────────────

@carddav_bp.route("/dav/addressbook/", methods=["REPORT"])
@dav_auth_required
def report_collection() -> Response:
    try:
        body_root = parse_report_body(request.get_data())
    except ValueError:
        return Response(_PAYLOAD_TOO_LARGE, status=413)
    if body_root is None:
        return Response("Bad Request", status=400)

    # Detect report type
    local = body_root.tag.split("}")[-1] if "}" in body_root.tag else body_root.tag

    if local == "sync-collection":
        return _handle_sync_collection(body_root)
    elif local == "addressbook-multiget":
        return _handle_multiget(body_root)
    elif local == "addressbook-query":
        return _handle_addressbook_query(body_root)

    return Response("Report type not supported", status=501)


def _handle_sync_collection(body_root: ET.Element) -> Response:
    """Handle DAV:sync-collection REPORT."""
    # Extract client's sync-token
    token_el = body_root.find(dav_tag("sync-token"))
    client_token = ""
    if token_el is not None and token_el.text:
        client_token = token_el.text.replace("data:,", "")

    multistatus = make_multistatus()
    current_token = get_sync_token(COLLECTION_NAME)

    if not client_token or client_token != current_token:
        # Full sync — return all current resources
        parties = list_parties()
        live_ids: set[str] = set()
        for partie in parties:
            live_ids.add(partie["id"])
            resp = add_response(multistatus, f"/dav/addressbook/{partie['id']}.vcf")
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = f'"{partie.get("etag", "")}"'

        # Report tombstones (deleted resources) — never for live resources,
        # or the same href would get both a 200 propstat and a 404 status
        # (RFC 6578 violation).
        tombstones = get_tombstones(COLLECTION_NAME)
        for ts in tombstones:
            if ts["id"] in live_ids:
                continue
            add_status_response(
                multistatus,
                f"/dav/addressbook/{ts['id']}.vcf",
                404,
                "Not Found",
            )

    # Include current sync-token
    ET.SubElement(multistatus, dav_tag("sync-token")).text = (
        f"data:,{current_token}"
    )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_multiget(body_root: ET.Element) -> Response:
    """Handle CardDAV:addressbook-multiget REPORT."""
    multistatus = make_multistatus()

    for href_el in body_root.findall(dav_tag("href")):
        href = href_el.text or ""
        # Extract partie_id from href
        partie_id = _extract_id_from_href(href)
        if not partie_id:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        partie = get_partie(partie_id)
        if not partie:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{partie.get("etag", "")}"'
        ET.SubElement(prop, carddav_tag("address-data")).text = (
            partie_to_vcard(partie)
        )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_addressbook_query(body_root: ET.Element) -> Response:
    """Handle CardDAV:addressbook-query REPORT (return all)."""
    multistatus = make_multistatus()
    parties = list_parties()

    for partie in parties:
        href = f"/dav/addressbook/{partie['id']}.vcf"
        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{partie.get("etag", "")}"'
        ET.SubElement(prop, carddav_tag("address-data")).text = (
            partie_to_vcard(partie)
        )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


# ── GET ────────────────────────────────────────────────────────────────────

@carddav_bp.route("/dav/addressbook/<partie_id>.vcf", methods=["GET"])
@dav_auth_required
def get_resource(partie_id: str) -> Response:
    partie = get_partie(partie_id)
    if not partie:
        return Response("Not Found", status=404)

    vcf = partie_to_vcard(partie)
    resp = Response(vcf, status=200, content_type="text/vcard; charset=utf-8")
    resp.headers["ETag"] = f'"{partie.get("etag", "")}"'
    return resp


# ── PUT ────────────────────────────────────────────────────────────────────

@carddav_bp.route("/dav/addressbook/<partie_id>.vcf", methods=["PUT"])
@dav_auth_required
def put_resource(partie_id: str) -> Response:
    # The URL names the resource, and a created contact is stored under that
    # very name — so a name that can never be a document id is refused
    # before any read (an existing contact necessarily has a valid id, so
    # this never refuses an update).
    if not valid_resource_id(partie_id):
        log_dav_operation("put", "addressbook", status_code=400,
                          reason="nom_invalide")
        return Response("Bad Request — identifiant de ressource invalide.",
                        status=400)

    # Conditional request handling
    if_match = request.headers.get("If-Match")
    if_none_match = request.headers.get("If-None-Match")

    existing = get_partie(partie_id)

    # If-None-Match: * means "only create, do not overwrite"
    if if_none_match == "*" and existing:
        return Response("Precondition Failed", status=412)

    # If-Match: compare etag
    if if_match and existing:
        existing_etag = f'"{existing.get("etag", "")}"'
        if if_match != existing_etag:
            return Response("Precondition Failed", status=412)

    # If-Match provided but resource doesn't exist
    if if_match and not existing:
        return Response("Precondition Failed", status=412)

    # Parse vCard body
    vcard_str = request.get_data(as_text=True)
    if not vcard_str:
        return Response("Bad Request", status=400)

    try:
        data = vcard_to_partie(vcard_str)
    except Exception:
        return Response("Bad Request — invalid vCard", status=400)

    if existing:
        # Update
        updated, errors = update_partie(partie_id, data)
        if errors:
            return _refused(partie_id, errors)
        bump_ctag(COLLECTION_NAME)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        # The URL names the new resource and the body carries its UID: the
        # contact is stored under BOTH, or the phone's href 404s on every
        # later GET/PUT and a duplicate with another UID syncs down.
        uid = data.pop("vcard_uid", None)
        created, errors = create_partie(data, dav_id=partie_id, dav_uid=uid)
        if errors == [DAV_ID_TAKEN]:
            # create() found a contact our fail-open read did not see (a
            # read error, or a racing PUT): refused, never overwritten.
            log_dav_operation("put", "addressbook", status_code=412,
                              reason="id_pris")
            return Response("Precondition Failed", status=412)
        if errors == [DAV_ID_INVALID]:
            log_dav_operation("put", "addressbook", status_code=400,
                              reason="nom_invalide")
            return Response(
                "Bad Request — identifiant de ressource invalide.", status=400
            )
        if errors:
            return _refused(partie_id, errors)
        # Resource (re)enters the collection — drop any stale tombstone
        remove_tombstone(COLLECTION_NAME, partie_id)
        bump_ctag(COLLECTION_NAME)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    # Prefer: return=minimal
    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp


def _refused(partie_id: str, errors: list[str]) -> Response:
    """The answer to a vCard the model refused.

    422 with the model's own French reasons in the body: « Données
    invalides. » alone hid the cause from a ``curl`` and from any client
    that surfaces the server's text — notably the refusal to change the
    role or type of a contact that represents another one. The LOG carries
    the count only: a refusal can name a contact (« Mandataire « … » »,
    « mandataire de … »), and the redaction filter never scrubs names.

    A reverse-reference check that could not RUN is not the vCard's fault:
    503 + ``Retry-After``, so the client retries the same PUT later instead
    of treating it as a permanent refusal.
    """
    logger.warning(
        "CardDAV PUT refused for %s: %d validation error(s)",
        sanitize_log_value(partie_id), len(errors),
    )
    if errors == [MANDATAIRE_CHECK_UNAVAILABLE]:
        resp = Response(
            MANDATAIRE_CHECK_UNAVAILABLE, status=503,
            content_type="text/plain; charset=utf-8",
        )
        resp.headers["Retry-After"] = "60"
        return resp
    return Response(
        "Données invalides : " + " ".join(errors), status=422,
        content_type="text/plain; charset=utf-8",
    )


# ── DELETE ─────────────────────────────────────────────────────────────────

@carddav_bp.route("/dav/addressbook/<partie_id>.vcf", methods=["DELETE"])
@dav_auth_required
def delete_resource(partie_id: str) -> Response:
    existing = get_partie(partie_id)
    if not existing:
        return Response("Not Found", status=404)

    # If-Match
    if_match = request.headers.get("If-Match")
    if if_match:
        existing_etag = f'"{existing.get("etag", "")}"'
        if if_match != existing_etag:
            return Response("Precondition Failed", status=412)

    success, error = delete_partie(partie_id)
    if not success:
        return _delete_refused(error)

    record_tombstone(COLLECTION_NAME, partie_id)
    bump_ctag(COLLECTION_NAME)
    # Append-only deletion trail (PA-G06) — a phone-side contact delete
    # must leave the same trace as a web one.
    record_deletion(
        "partie", partie_id,
        title=display_name(existing),
        status=existing.get("contact_role", ""),
    )
    return Response("", status=204)


def _delete_refused(error: str) -> Response:
    """The answer to a DELETE the model refused.

    Each refusal gets its own status, and the log line carries a machine
    ``reason`` — never the text: a reference conflict NAMES the represented
    contacts (« … mandataire de Sophie Gagnon »), and the redaction filter
    never scrubs names. (Until the lot 0b review this path logged that text
    at ERROR and answered 500 « Erreur serveur. » to a business refusal.)

    * the contact vanished between the two reads → 404;
    * the reference check could not RUN → 503 + ``Retry-After`` (not the
      client's fault: the same DELETE succeeds later);
    * the store refused the write → 500 (``delete_partie`` already logged
      an ``unexpected`` ERROR with the traceback);
    * every other refusal is a CONFLICT with the contact's current state
      (still linked to a dossier, or the mandataire of another contact) →
      409 with the French reason in the body.
    """
    if error == PARTIE_NOT_FOUND:
        status, reason = 404, "introuvable"
    elif error == PARTIE_DELETE_CHECK_UNAVAILABLE:
        status, reason = 503, "verification_indisponible"
    elif error == PARTIE_DELETE_FAILED:
        status, reason = 500, "ecriture_echouee"
    else:
        status, reason = 409, "references"
    log_dav_operation("delete", "addressbook", status_code=status,
                      reason=reason)
    resp = Response(error, status=status,
                    content_type="text/plain; charset=utf-8")
    if status == 503:
        resp.headers["Retry-After"] = "60"
    return resp


# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_id_from_href(href: str) -> str | None:
    """Extract the resource ID from a CardDAV href like /dav/addressbook/<id>.vcf."""
    href = href.rstrip("/")
    if not href.endswith(".vcf"):
        return None
    segment = href.rsplit("/", 1)[-1]
    # The trailing extension only: ``replace()`` removed EVERY occurrence,
    # so a client-chosen name holding « .vcf » resolved to another id.
    return segment.removesuffix(".vcf")
