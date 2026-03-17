"""CalDAV server — RFC 4791 endpoints for hearings (VEVENT).

Endpoints
---------
/dav/calendar/                     Calendar collection
/dav/calendar/<hearing_id>.ics     Individual event resource
"""

import xml.etree.ElementTree as ET

from flask import Blueprint, Response, request

from dav.dav_auth import dav_auth_required
from dav.sync import bump_ctag, get_ctag, get_sync_token, get_tombstones, record_tombstone
from dav.xml_utils import (
    DAV_NS,
    CALDAV_NS,
    add_propstat,
    add_response,
    add_status_response,
    caldav_tag,
    cs_tag,
    dav_tag,
    make_multistatus,
    parse_propfind_body,
    parse_report_body,
    propfind_requests_prop,
    serialize_multistatus,
)
from models.hearing import (
    create_hearing,
    delete_hearing,
    get_hearing,
    hearing_to_vevent,
    list_hearings,
    update_hearing,
    vevent_to_hearing,
)

caldav_bp = Blueprint("caldav", __name__)

COLLECTION_NAME = "hearings"
COLLECTION_PATH = "/dav/calendar/"


# ── OPTIONS ────────────────────────────────────────────────────────────────

@caldav_bp.route("/dav/calendar/", methods=["OPTIONS"])
@caldav_bp.route("/dav/calendar/<hearing_id>.ics", methods=["OPTIONS"])
@dav_auth_required
def options(hearing_id: str = None) -> Response:
    resp = Response("", status=200)
    resp.headers["Allow"] = "OPTIONS, GET, PUT, DELETE, PROPFIND, REPORT"
    resp.headers["DAV"] = "1, 2, 3, calendar-access"
    return resp


# ── PROPFIND ───────────────────────────────────────────────────────────────

@caldav_bp.route("/dav/calendar/", methods=["PROPFIND"])
@dav_auth_required
def propfind_collection() -> Response:
    depth = request.headers.get("Depth", "0")
    body = parse_propfind_body(request.get_data())
    multistatus = make_multistatus()

    _add_collection_response(multistatus, body)

    if depth == "1":
        hearings = list_hearings()
        for hearing in hearings:
            _add_resource_response(multistatus, hearing, body)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


@caldav_bp.route("/dav/calendar/<hearing_id>.ics", methods=["PROPFIND"])
@dav_auth_required
def propfind_resource(hearing_id: str) -> Response:
    hearing = get_hearing(hearing_id)
    if not hearing:
        return Response("Not Found", status=404)

    body = parse_propfind_body(request.get_data())
    multistatus = make_multistatus()
    _add_resource_response(multistatus, hearing, body)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _add_collection_response(
    multistatus: ET.Element, body: ET.Element | None
) -> None:
    resp = add_response(multistatus, COLLECTION_PATH)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        rt = ET.SubElement(prop, dav_tag("resourcetype"))
        ET.SubElement(rt, dav_tag("collection"))
        ET.SubElement(rt, caldav_tag("calendar"))

    if propfind_requests_prop(body, dav_tag("displayname")):
        ET.SubElement(prop, dav_tag("displayname")).text = (
            "Pallas Athena \u2014 Audiences"
        )

    if propfind_requests_prop(body, cs_tag("getctag")):
        ET.SubElement(prop, cs_tag("getctag")).text = get_ctag(COLLECTION_NAME)

    if propfind_requests_prop(body, dav_tag("sync-token")):
        ET.SubElement(prop, dav_tag("sync-token")).text = (
            f"data:,{get_sync_token(COLLECTION_NAME)}"
        )

    # supported-calendar-component-set
    if propfind_requests_prop(
        body, caldav_tag("supported-calendar-component-set")
    ):
        sccs = ET.SubElement(prop, caldav_tag("supported-calendar-component-set"))
        comp = ET.SubElement(sccs, caldav_tag("comp"))
        comp.set("name", "VEVENT")

    if propfind_requests_prop(body, dav_tag("supported-report-set")):
        srs = ET.SubElement(prop, dav_tag("supported-report-set"))
        sr = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr, dav_tag("report")).append(
            ET.Element(dav_tag("sync-collection"))
        )
        sr2 = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr2, dav_tag("report")).append(
            ET.Element(caldav_tag("calendar-multiget"))
        )
        sr3 = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr3, dav_tag("report")).append(
            ET.Element(caldav_tag("calendar-query"))
        )


def _add_resource_response(
    multistatus: ET.Element,
    hearing: dict,
    body: ET.Element | None,
) -> None:
    href = f"/dav/calendar/{hearing['id']}.ics"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("getetag")):
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{hearing.get("etag", "")}"'

    if propfind_requests_prop(body, dav_tag("getcontenttype")):
        ET.SubElement(prop, dav_tag("getcontenttype")).text = (
            "text/calendar; charset=utf-8"
        )

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        ET.SubElement(prop, dav_tag("resourcetype"))

    if body is not None and propfind_requests_prop(
        body, caldav_tag("calendar-data")
    ):
        ET.SubElement(prop, caldav_tag("calendar-data")).text = (
            hearing_to_vevent(hearing)
        )


# ── REPORT ─────────────────────────────────────────────────────────────────

@caldav_bp.route("/dav/calendar/", methods=["REPORT"])
@dav_auth_required
def report_collection() -> Response:
    body_root = parse_report_body(request.get_data())
    if body_root is None:
        return Response("Bad Request", status=400)

    local = body_root.tag.split("}")[-1] if "}" in body_root.tag else body_root.tag

    if local == "sync-collection":
        return _handle_sync_collection(body_root)
    elif local == "calendar-multiget":
        return _handle_multiget(body_root)
    elif local == "calendar-query":
        return _handle_calendar_query(body_root)

    return Response("Report type not supported", status=501)


def _handle_sync_collection(body_root: ET.Element) -> Response:
    token_el = body_root.find(dav_tag("sync-token"))
    client_token = ""
    if token_el is not None and token_el.text:
        client_token = token_el.text.replace("data:,", "")

    multistatus = make_multistatus()
    current_token = get_sync_token(COLLECTION_NAME)

    if not client_token or client_token != current_token:
        hearings = list_hearings()
        for hearing in hearings:
            resp = add_response(multistatus, f"/dav/calendar/{hearing['id']}.ics")
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = (
                f'"{hearing.get("etag", "")}"'
            )

        tombstones = get_tombstones(COLLECTION_NAME)
        for ts in tombstones:
            add_status_response(
                multistatus,
                f"/dav/calendar/{ts['id']}.ics",
                404,
                "Not Found",
            )

    ET.SubElement(multistatus, dav_tag("sync-token")).text = (
        f"data:,{current_token}"
    )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_multiget(body_root: ET.Element) -> Response:
    multistatus = make_multistatus()

    for href_el in body_root.findall(dav_tag("href")):
        href = href_el.text or ""
        hearing_id = _extract_id_from_href(href)
        if not hearing_id:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        hearing = get_hearing(hearing_id)
        if not hearing:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = (
            f'"{hearing.get("etag", "")}"'
        )
        ET.SubElement(prop, caldav_tag("calendar-data")).text = (
            hearing_to_vevent(hearing)
        )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_calendar_query(body_root: ET.Element) -> Response:
    multistatus = make_multistatus()
    hearings = list_hearings()

    for hearing in hearings:
        href = f"/dav/calendar/{hearing['id']}.ics"
        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = (
            f'"{hearing.get("etag", "")}"'
        )
        ET.SubElement(prop, caldav_tag("calendar-data")).text = (
            hearing_to_vevent(hearing)
        )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


# ── GET ────────────────────────────────────────────────────────────────────

@caldav_bp.route("/dav/calendar/<hearing_id>.ics", methods=["GET"])
@dav_auth_required
def get_resource(hearing_id: str) -> Response:
    hearing = get_hearing(hearing_id)
    if not hearing:
        return Response("Not Found", status=404)

    ical = hearing_to_vevent(hearing)
    resp = Response(ical, status=200, content_type="text/calendar; charset=utf-8")
    resp.headers["ETag"] = f'"{hearing.get("etag", "")}"'
    return resp


# ── PUT ────────────────────────────────────────────────────────────────────

@caldav_bp.route("/dav/calendar/<hearing_id>.ics", methods=["PUT"])
@dav_auth_required
def put_resource(hearing_id: str) -> Response:
    if_match = request.headers.get("If-Match")
    if_none_match = request.headers.get("If-None-Match")

    existing = get_hearing(hearing_id)

    if if_none_match == "*" and existing:
        return Response("Precondition Failed", status=412)
    if if_match and existing:
        existing_etag = f'"{existing.get("etag", "")}"'
        if if_match != existing_etag:
            return Response("Precondition Failed", status=412)
    if if_match and not existing:
        return Response("Precondition Failed", status=412)

    ical_str = request.get_data(as_text=True)
    if not ical_str:
        return Response("Bad Request", status=400)

    try:
        data = vevent_to_hearing(ical_str)
    except Exception:
        return Response("Bad Request — invalid iCalendar", status=400)

    if existing:
        updated, errors = update_hearing(hearing_id, data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(COLLECTION_NAME)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = hearing_id
        created, errors = create_hearing(data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(COLLECTION_NAME)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp


# ── DELETE ─────────────────────────────────────────────────────────────────

@caldav_bp.route("/dav/calendar/<hearing_id>.ics", methods=["DELETE"])
@dav_auth_required
def delete_resource(hearing_id: str) -> Response:
    existing = get_hearing(hearing_id)
    if not existing:
        return Response("Not Found", status=404)

    if_match = request.headers.get("If-Match")
    if if_match:
        existing_etag = f'"{existing.get("etag", "")}"'
        if if_match != existing_etag:
            return Response("Precondition Failed", status=412)

    success, error = delete_hearing(hearing_id)
    if not success:
        return Response(error, status=500)

    record_tombstone(COLLECTION_NAME, hearing_id)
    bump_ctag(COLLECTION_NAME)
    return Response("", status=204)


# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_id_from_href(href: str) -> str | None:
    href = href.rstrip("/")
    if not href.endswith(".ics"):
        return None
    segment = href.rsplit("/", 1)[-1]
    return segment.replace(".ics", "")
