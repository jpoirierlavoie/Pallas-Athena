"""CardDAV server — RFC 6352 endpoints for contacts (parties).

Endpoints
---------
/dav/addressbook/                   Address-book collection
/dav/addressbook/<partie_id>.vcf    Individual contact resource
"""

import xml.etree.ElementTree as ET

from flask import Blueprint, Response, request

from dav.dav_auth import dav_auth_required
from dav.sync import bump_ctag, get_ctag, get_sync_token, get_tombstones, record_tombstone
from dav.xml_utils import (
    DAV_NS,
    CARDDAV_NS,
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
from models.partie import (
    create_partie,
    delete_partie,
    get_partie,
    list_parties,
    partie_to_vcard,
    update_partie,
    vcard_to_partie,
)

carddav_bp = Blueprint("carddav", __name__)

COLLECTION_NAME = "parties"
COLLECTION_PATH = "/dav/addressbook/"


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
    body = parse_propfind_body(request.get_data())
    multistatus = make_multistatus()

    # Collection response (always included)
    _add_collection_response(multistatus, body)

    # Depth:1 — include individual resources
    if depth == "1":
        parties = list_parties()
        for partie in parties:
            _add_resource_response(multistatus, partie, body)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


@carddav_bp.route("/dav/addressbook/<partie_id>.vcf", methods=["PROPFIND"])
@dav_auth_required
def propfind_resource(partie_id: str) -> Response:
    partie = get_partie(partie_id)
    if not partie:
        return Response("Not Found", status=404)

    body = parse_propfind_body(request.get_data())
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
    body_root = parse_report_body(request.get_data())
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
        for partie in parties:
            resp = add_response(multistatus, f"/dav/addressbook/{partie['id']}.vcf")
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = f'"{partie.get("etag", "")}"'

        # Report tombstones (deleted resources)
        tombstones = get_tombstones(COLLECTION_NAME)
        for ts in tombstones:
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
            return Response("\n".join(errors), status=422)
        bump_ctag(COLLECTION_NAME)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        # Create with the ID from the URL
        data["id"] = partie_id
        created, errors = create_partie(data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(COLLECTION_NAME)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    # Prefer: return=minimal
    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp


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
        return Response(error, status=500)

    record_tombstone(COLLECTION_NAME, partie_id)
    bump_ctag(COLLECTION_NAME)
    return Response("", status=204)


# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_id_from_href(href: str) -> str | None:
    """Extract the resource ID from a CardDAV href like /dav/addressbook/<id>.vcf."""
    href = href.rstrip("/")
    if not href.endswith(".vcf"):
        return None
    segment = href.rsplit("/", 1)[-1]
    return segment.replace(".vcf", "")
