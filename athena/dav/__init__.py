"""DAV protocol endpoints — CardDAV, CalDAV, RFC-5545.

This module registers a top-level ``dav_bp`` blueprint that:
1. Provides ``/.well-known/carddav`` and ``/.well-known/caldav`` redirects.
2. Handles PROPFIND on the DAV root (``/dav/``) for principal/collection
   discovery so DavX5 can locate all four sub-collections.
3. Imports and registers the sub-blueprints for each protocol.
"""

import xml.etree.ElementTree as ET

from flask import Blueprint, Response, redirect, request

from dav.dav_auth import dav_auth_required
from dav.xml_utils import (
    add_propstat,
    add_response,
    caldav_tag,
    cs_tag,
    dav_tag,
    make_multistatus,
    parse_propfind_body,
    propfind_requests_prop,
    serialize_multistatus,
)

dav_bp = Blueprint("dav", __name__)


# ── Well-known redirects (DavX5 discovery) ────────────────────────────────

@dav_bp.route("/.well-known/carddav", methods=["GET", "PROPFIND"])
def well_known_carddav() -> Response:
    return redirect("/dav/", code=301)


@dav_bp.route("/.well-known/caldav", methods=["GET", "PROPFIND"])
def well_known_caldav() -> Response:
    return redirect("/dav/", code=301)


# ── DAV root (principal discovery) ────────────────────────────────────────

@dav_bp.route("/dav/", methods=["OPTIONS"])
@dav_auth_required
def dav_root_options() -> Response:
    resp = Response("", status=200)
    resp.headers["Allow"] = "OPTIONS, PROPFIND"
    resp.headers["DAV"] = "1, 2, 3, addressbook, calendar-access"
    return resp


@dav_bp.route("/dav/", methods=["PROPFIND"])
@dav_auth_required
def dav_root_propfind() -> Response:
    """Return the principal resource and advertise available collections.

    DavX5 issues PROPFIND on /dav/ to discover which collections exist.
    We respond with the root plus child collection hrefs.
    """
    depth = request.headers.get("Depth", "0")
    body = parse_propfind_body(request.get_data())
    multistatus = make_multistatus()

    # Root resource
    root_resp = add_response(multistatus, "/dav/")
    prop = add_propstat(root_resp)

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        rt = ET.SubElement(prop, dav_tag("resourcetype"))
        ET.SubElement(rt, dav_tag("collection"))

    if propfind_requests_prop(body, dav_tag("displayname")):
        ET.SubElement(prop, dav_tag("displayname")).text = "Pallas Athena"

    if propfind_requests_prop(body, dav_tag("current-user-principal")):
        cup = ET.SubElement(prop, dav_tag("current-user-principal"))
        ET.SubElement(cup, dav_tag("href")).text = "/dav/"

    # Tell DavX5 where to find address book and calendars
    if propfind_requests_prop(body, dav_tag("addressbook-home-set")) or propfind_requests_prop(body, "{urn:ietf:params:xml:ns:carddav}addressbook-home-set"):
        from dav.xml_utils import carddav_tag
        ahs = ET.SubElement(prop, carddav_tag("addressbook-home-set"))
        ET.SubElement(ahs, dav_tag("href")).text = "/dav/addressbook/"

    if propfind_requests_prop(body, dav_tag("calendar-home-set")) or propfind_requests_prop(body, caldav_tag("calendar-home-set")):
        chs = ET.SubElement(prop, caldav_tag("calendar-home-set"))
        ET.SubElement(chs, dav_tag("href")).text = "/dav/"

    # Depth:1 — include child collections
    if depth == "1":
        for coll_path, coll_name in [
            ("/dav/addressbook/", "Clients"),
            ("/dav/calendar/", "Audiences"),
            ("/dav/tasks/", "T\u00e2ches"),
            ("/dav/journals/", "Dossiers"),
        ]:
            child = add_response(multistatus, coll_path)
            child_prop = add_propstat(child)
            rt = ET.SubElement(child_prop, dav_tag("resourcetype"))
            ET.SubElement(rt, dav_tag("collection"))
            ET.SubElement(child_prop, dav_tag("displayname")).text = (
                f"Pallas Athena \u2014 {coll_name}"
            )

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")
